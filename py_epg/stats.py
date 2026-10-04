#!/usr/bin/env python3
"""'--stats' mode: prints key statistics for an XMLTV file.

Reads the file streaming (iterparse) so multi-MB guides stay cheap.
"""

import json
import os
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from lxml import etree as ET

TOP_CHANNELS = 10


def _parse_time(s):
    """XMLTV timestamp 'YYYYMMDDHHMMSS +ZZZZ' -> aware datetime."""
    s = (s or '').strip()
    if not s:
        return None
    for fmt in ('%Y%m%d%H%M%S %z', '%Y%m%d%H%M%S'):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        # XMLTV requires a timezone; treat a missing one as UTC so it
        # stays comparable with the aware coverage window
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _site(channel_id):
    """'TVCHANNEL-4.PORT.HU' -> 'PORT.HU'."""
    parts = (channel_id or '').split('.', 1)
    return parts[1] if len(parts) > 1 else '(none)'


def _covered_seconds(intervals, win_start, win_stop):
    """Union length of (start, stop) intervals clipped to the window."""
    covered = 0.0
    cur_start = cur_stop = None
    for start, stop in intervals:
        if start is None or stop is None or \
                stop <= win_start or start >= win_stop:
            continue
        start = max(start, win_start)
        stop = min(stop, win_stop)
        if cur_start is None:
            cur_start, cur_stop = start, stop
        elif start <= cur_stop:
            cur_stop = max(cur_stop, stop)
        else:
            covered += (cur_stop - cur_start).total_seconds()
            cur_start, cur_stop = start, stop
    if cur_start is not None:
        covered += (cur_stop - cur_start).total_seconds()
    return covered


def collect(path, now=None):
    """Parses the XMLTV file and returns a JSON-friendly stats dict."""
    now = now or datetime.now(timezone.utc)
    channels = {}                 # id -> {name, icon}
    intervals = defaultdict(list)  # channel id -> [(start, stop)]
    field_counts = Counter()      # programme child tag -> count
    per_site = Counter()
    per_day = Counter()
    slots = Counter()             # (channel, start, stop) -> n, for dupes
    missing = Counter()
    earliest = latest = None
    generator = xmltv_date = None

    for event, elem in ET.iterparse(path, events=('start', 'end')):
        if event == 'start' and elem.tag == 'tv':
            generator = elem.get('generator-info-name')
            xmltv_date = elem.get('date')
            continue
        if event != 'end':
            continue
        if elem.tag == 'channel':
            name_el = elem.find('display-name')
            channels[elem.get('id')] = {
                'name': (name_el.text or '').strip()
                if name_el is not None else '',
                'icon': elem.find('icon') is not None,
            }
        elif elem.tag == 'programme':
            ch = elem.get('channel')
            start = _parse_time(elem.get('start'))
            stop = _parse_time(elem.get('stop'))
            slots[(ch, elem.get('start'), elem.get('stop'))] += 1
            if ch is not None:
                intervals[ch].append((start, stop))
                per_site[_site(ch)] += 1
            if start is None:
                missing['start'] += 1
            else:
                per_day[start.strftime('%Y-%m-%d')] += 1
                if earliest is None or start < earliest:
                    earliest = start
            if stop is None:
                missing['stop'] += 1
            elif latest is None or stop > latest:
                latest = stop
            if elem.find('title') is None:
                missing['title'] += 1
            # presence per programme, not element count - repeated
            # elements (multi-language titles, ratings) count once
            field_counts.update({child.tag for child in elem})
        else:
            continue  # keep leaf text/attrs for the parent element
        elem.clear()
        # drop already-processed siblings so memory stays flat
        while elem.getprevious() is not None:
            del elem.getparent()[0]

    total_programmes = sum(slots.values())
    duplicates = total_programmes - len(slots)
    zero_length = sum(
        n for (c, s, e), n in slots.items()
        if s is not None and s == e)

    # next-24h coverage: union of each channel's intervals vs [now, +24h]
    win_stop = now + timedelta(hours=24)
    win_secs = (win_stop - now).total_seconds()
    cover_pct = {}
    for cid in channels:
        pct = 100.0 * _covered_seconds(
            sorted(intervals.get(cid, [])), now, win_stop) / win_secs
        cover_pct[cid] = min(pct, 100.0)
    covered_any = sum(1 for p in cover_pct.values() if p > 0)
    covered_full = sum(1 for p in cover_pct.values() if p >= 99.0)

    with_programmes = sum(1 for c in channels if intervals.get(c))
    prog_count = {c: len(iv) for c, iv in intervals.items()}

    return {
        'file': path,
        'size_bytes': os.path.getsize(path),
        'generator': generator,
        'xmltv_date': xmltv_date,
        'channels': {
            'total': len(channels),
            'with_icon': sum(1 for c in channels.values() if c['icon']),
            'by_site': dict(Counter(
                _site(cid) for cid in channels).most_common()),
            'with_programmes': with_programmes,
            'without_programmes': sorted(
                c for c in channels if not intervals.get(c)),
        },
        'programmes': {
            'total': total_programmes,
            'by_site': dict(per_site.most_common()),
            'duplicates': duplicates,
            'zero_length': zero_length,
            'missing_start': missing['start'],
            'missing_stop': missing['stop'],
            'missing_title': missing['title'],
        },
        'fields': dict(field_counts.most_common()),
        'window': {
            'start': earliest.isoformat() if earliest else None,
            'stop': latest.isoformat() if latest else None,
            'days': round((latest - earliest).total_seconds() / 86400, 1)
            if earliest and latest else None,
            'per_day': dict(sorted(per_day.items())),
        },
        'next_24h': {
            'from': now.isoformat(),
            'channels_with_any': covered_any,
            'channels_fully_covered': covered_full,
            'avg_coverage_pct': round(
                sum(cover_pct.values()) / len(cover_pct), 1)
            if cover_pct else 0.0,
            'low_coverage': [
                {'id': c, 'coverage_pct': round(cover_pct[c], 1)}
                for c in sorted(channels)
                if 0 < cover_pct[c] < 99.0],
            'no_coverage': sorted(
                c for c in channels if cover_pct[c] == 0),
        },
        'top_channels': [
            {'id': c, 'name': channels[c]['name'],
             'programmes': prog_count[c]}
            for c in sorted(prog_count, key=prog_count.get,
                            reverse=True)[:TOP_CHANNELS]
            if c in channels],
    }


def _pct(part, whole):
    return f'{100 * part / whole:.0f}%' if whole else '0%'


def _size(num_bytes):
    for unit in ('B', 'KB', 'MB'):
        if num_bytes < 1024 or unit == 'MB':
            return f'{num_bytes:.1f} {unit}' \
                if unit != 'B' else f'{num_bytes} B'
        num_bytes /= 1024


def _wrapped(label, items, indent='    ', width=78):
    lines = textwrap.wrap(', '.join(items), width=width - len(indent))
    return '\n'.join(f'{indent}{line}' for line in lines) or f'{indent}-'


def format_stats(stats):
    out = []
    ch = stats['channels']
    pr = stats['programmes']
    win = stats['window']
    n24 = stats['next_24h']
    gen = f", {stats['generator']}" if stats['generator'] else ''
    out.append(f"{stats['file']} ({_size(stats['size_bytes'])}{gen})")
    out.append('')
    out.append('CHANNELS')
    sites = ', '.join(f'{n} {s}' for s, n in ch['by_site'].items())
    out.append(f'  {ch["total"]} total ({sites}), '
               f'{ch["with_icon"]} with icon')
    out.append(f'  {ch["with_programmes"]} with programmes')
    empty = ch['without_programmes']
    if empty:
        out.append(f'  {len(empty)} with NO programmes:')
        out.append(_wrapped('', empty))
    out.append('')
    out.append('PROGRAMMES')
    sites = ', '.join(f'{n} {s}' for s, n in pr['by_site'].items())
    out.append(f'  {pr["total"]:,} total ({sites})')
    out.append(
        f'  duplicates: {pr["duplicates"]:,} | '
        f'zero-length: {pr["zero_length"]:,} | '
        f'missing stop: {pr["missing_stop"]:,} | '
        f'missing title: {pr["missing_title"]:,}')
    if stats['fields']:
        out.append('')
        out.append('METADATA FIELDS (share of programmes)')
        for name, n in stats['fields'].items():
            out.append(f'  {name:<14} {n:>7,} '
                       f'({_pct(n, pr["total"])})')
    out.append('')
    out.append('TIME WINDOW')
    if win['start']:
        out.append(f"  {win['start']} -> {win['stop']} "
                   f"({win['days']} days)")
        days = [f'{d[5:]}: {n:,}' for d, n in win['per_day'].items()]
        out.append(_wrapped('', days))
    else:
        out.append('  (no programmes)')
    out.append('')
    asof = n24['from'][:16].replace('T', ' ')
    out.append(f'NEXT 24H COVERAGE (as of {asof} UTC)')
    out.append(
        f'  {n24["channels_with_any"]}/{ch["total"]} channels with any '
        f'coverage, {n24["channels_fully_covered"]} fully covered '
        f'(avg {n24["avg_coverage_pct"]}% across all channels)')
    low = n24['low_coverage']
    if low:
        out.append('  low coverage:')
        out.append(_wrapped(
            '', [f"{c['id']} {c['coverage_pct']}%" for c in low]))
    if stats['top_channels']:
        out.append('')
        out.append('TOP CHANNELS BY PROGRAMMES')
        for c in stats['top_channels']:
            name = f'  {c["name"]}' if c['name'] else ''
            out.append(f"  {c['id']:<28} {c['programmes']:>7,}{name}")
    return '\n'.join(out)


def print_stats(path, json_out=False):
    stats = collect(path)
    if json_out:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print(format_stats(stats))
