#!/usr/bin/env python3
"""Tests for the --stats mode (py_epg/stats.py)."""

import json
from datetime import datetime, timezone

from py_epg.stats import collect, format_stats, print_stats

XMLTV = '''<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="test" date="20240115000000">
  <channel id="A.TVMUSTRA.HU">
    <display-name>Alpha</display-name>
    <icon src="http://x/a.png"/>
  </channel>
  <channel id="B.PORT.HU"><display-name>Beta</display-name></channel>
  <channel id="C.PORT.HU"><display-name>Empty</display-name></channel>
  <programme start="20240115060000 +0100" stop="20240115070000 +0100"
             channel="A.TVMUSTRA.HU">
    <title lang="hu">Show</title>
    <desc>x</desc><icon src="http://x/i.png"/><category>d</category>
  </programme>
  <programme start="20240115060000 +0100" stop="20240115070000 +0100"
             channel="A.TVMUSTRA.HU">
    <title lang="hu">Show</title>
  </programme>
  <programme start="20240115070000 +0100" channel="A.TVMUSTRA.HU">
    <title lang="hu">NoStop</title>
  </programme>
  <programme start="20240116060000 +0100" stop="20240116080000 +0100"
             channel="B.PORT.HU">
    <title lang="hu">B show</title>
  </programme>
  <programme start="20240116060000 +0100" stop="20240116060000 +0100"
             channel="B.PORT.HU">
    <title lang="hu">ZeroLen</title>
  </programme>
</tv>'''


def make_stats(tmp_path, now=None):
    path = tmp_path / 'epg.xml'
    path.write_text(XMLTV)
    return collect(str(path), now=now or datetime(
        2024, 1, 15, 12, tzinfo=timezone.utc))


class TestCollect:
    def test_channel_counts(self, tmp_path):
        s = make_stats(tmp_path)
        assert s['channels']['total'] == 3
        assert s['channels']['with_programmes'] == 2
        assert s['channels']['without_programmes'] == ['C.PORT.HU']
        assert s['channels']['with_icon'] == 1
        assert s['channels']['by_site'] == {
            'PORT.HU': 2, 'TVMUSTRA.HU': 1}

    def test_programme_counts(self, tmp_path):
        s = make_stats(tmp_path)['programmes']
        assert s['total'] == 5
        assert s['duplicates'] == 1          # identical A slot x2
        assert s['zero_length'] == 1         # B 06:00 -> 06:00
        assert s['missing_stop'] == 1        # A 07:00 NoStop
        assert s['missing_title'] == 0
        assert s['missing_start'] == 0

    def test_fields_count_presence(self, tmp_path):
        f = make_stats(tmp_path)['fields']
        assert f['title'] == 5               # every programme once
        assert f['desc'] == 1
        assert f['icon'] == 1

    def test_window_and_per_day(self, tmp_path):
        w = make_stats(tmp_path)['window']
        assert w['start'].startswith('2024-01-15')
        assert w['stop'].startswith('2024-01-16')
        assert w['per_day'] == {'2024-01-15': 3, '2024-01-16': 2}

    def test_next_24h_coverage(self, tmp_path):
        # 'now' = 2024-01-15 12:00 UTC. A's programmes are all before
        # noon UTC; B's 16th 06:00-08:00 +0100 = 05:00-07:00 UTC falls
        # inside [15th 12:00, 16th 12:00] -> 2h/24h = 8.33%.
        s = make_stats(tmp_path)
        n24 = s['next_24h']
        assert n24['channels_with_any'] == 1
        assert n24['channels_fully_covered'] == 0
        assert n24['low_coverage'] == [
            {'id': 'B.PORT.HU', 'coverage_pct': 8.3}]
        assert set(n24['no_coverage']) == {
            'A.TVMUSTRA.HU', 'C.PORT.HU'}

    def test_top_channels(self, tmp_path):
        top = make_stats(tmp_path)['top_channels']
        assert top[0]['id'] == 'A.TVMUSTRA.HU'
        assert top[0]['programmes'] == 3
        assert top[0]['name'] == 'Alpha'


class TestFormat:
    def test_readable_output(self, tmp_path):
        s = make_stats(tmp_path)
        text = format_stats(s)
        assert 'CHANNELS' in text
        assert '3 total' in text
        assert 'C.PORT.HU' in text           # empty channel listed
        assert 'duplicates: 1' in text
        assert 'NEXT 24H COVERAGE' in text

    def test_json_roundtrip(self, tmp_path):
        s = make_stats(tmp_path)
        assert json.loads(json.dumps(s))['channels']['total'] == 3


class TestEdgeCases:
    def stats_for(self, tmp_path, xml, **kw):
        path = tmp_path / 'e.xml'
        path.write_text(xml)
        return collect(str(path), **kw)

    def test_naive_timestamp_accepted(self, tmp_path):
        s = self.stats_for(tmp_path, '<tv><channel id="A"/>'
            '<programme start="20240115060000" stop="20240115070000"'
            ' channel="A"><title>x</title></programme></tv>')
        assert s['programmes']['total'] == 1
        assert s['window']['start'] == '2024-01-15T06:00:00+00:00'

    def test_unparseable_times_counted_missing(self, tmp_path):
        s = self.stats_for(tmp_path, '<tv><channel id="A"/>'
            '<programme start="garbage" channel="A">'
            '<title>x</title></programme></tv>')
        assert s['programmes']['missing_start'] == 1
        assert s['programmes']['missing_stop'] == 1
        assert s['window']['start'] is None

    def test_gap_between_intervals_counts_once(self, tmp_path):
        # two disjoint 1h blocks in the window -> 2h/24h = 8.3%
        s = self.stats_for(
            tmp_path, '<tv><channel id="A"/>'
            '<programme start="20240115130000 +0000"'
            ' stop="20240115140000 +0000" channel="A">'
            '<title>x</title></programme>'
            '<programme start="20240115160000 +0000"'
            ' stop="20240115170000 +0000" channel="A">'
            '<title>y</title></programme></tv>',
            now=datetime(2024, 1, 15, 12, tzinfo=timezone.utc))
        assert s['next_24h']['low_coverage'] == [
            {'id': 'A', 'coverage_pct': 8.3}]

    def test_overlapping_intervals_not_double_counted(self, tmp_path):
        # two overlapping 2h blocks covering the same hour -> 2h total
        s = self.stats_for(
            tmp_path, '<tv><channel id="A"/>'
            '<programme start="20240115130000 +0000"'
            ' stop="20240115150000 +0000" channel="A">'
            '<title>x</title></programme>'
            '<programme start="20240115140000 +0000"'
            ' stop="20240115160000 +0000" channel="A">'
            '<title>y</title></programme></tv>',
            now=datetime(2024, 1, 15, 12, tzinfo=timezone.utc))
        assert s['next_24h']['low_coverage'] == [
            {'id': 'A', 'coverage_pct': 12.5}]

    def test_empty_file(self, tmp_path, capsys):
        s = self.stats_for(tmp_path, '<tv/>')
        assert s['channels']['total'] == 0
        assert s['programmes']['total'] == 0
        assert s['window']['start'] is None
        assert '(no programmes)' in format_stats(s)


class TestPrintStats:
    def test_text_output(self, tmp_path, capsys):
        path = tmp_path / 'e.xml'
        path.write_text(XMLTV)
        print_stats(str(path))
        assert 'PROGRAMMES' in capsys.readouterr().out

    def test_json_output(self, tmp_path, capsys):
        path = tmp_path / 'e.xml'
        path.write_text(XMLTV)
        print_stats(str(path), json_out=True)
        out = json.loads(capsys.readouterr().out)
        assert out['programmes']['total'] == 5
