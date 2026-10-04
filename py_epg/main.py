#!/usr/bin/env python3

import argparse
import logging
import os
import pathlib
import time
from collections import defaultdict
from datetime import date, timedelta
from multiprocessing import Pool, current_process
from pprint import pprint
from typing import Dict, List, Set, Tuple

import requests
import tqdm
from dateutil.parser import parse
from lxml import etree as ET
from xmltv.models import (Category, Channel, Country, Desc, Icon,
                          Programme, Rating, SubTitle, Title, Tv)

from py_epg.common.cache import Cache
from py_epg.common.xmltv_writer import write_file_from_xml
from py_epg.common.epg_scraper import EpgScraper, UA
from py_epg.common.metadata import build_metadata
from py_epg.common.multiprocess_helper import setup_ltree_pickling
from py_epg.common.proxy import ProxyPool, get_proxy_session
from py_epg.common.requests import get_http_session
from py_epg.common.types import ChannelKey
from py_epg.common.utils import argparse_str2bool
from py_epg.scrapers import *
from py_epg.metadata_providers import *

DEFAULT_POOL_SIZE = 1
PBAR_NAME_COL_WIDTH = 15


def _remove_console_handler(logger: logging.Logger):
    for h in logger.handlers[:]:
        if isinstance(h, logging.StreamHandler) and \
                not isinstance(h, logging.FileHandler):
            logger.removeHandler(h)


def _init_worker(disable_console_log: bool):
    # Spawned workers re-run dictConfig on import, restoring the console
    # handler the parent removed - their log output would collide with the
    # parent's progress bars. Remove it again here.
    if disable_console_log:
        _remove_console_handler(logging.getLogger())


class PyEPG:
    """Main class of PyEPG"""

    def __init__(self):
        self._args = self._parse_args()
        self._log = logging.getLogger(__name__)
        self._config = self._read_config()
        self._cache = self._build_cache()
        self._cache.delete_expired()
        self._proxy = self._build_proxy()
        self._metadata = self._build_metadata()
        self._epg_scrapers = self._init_epg_scrapers()
        setup_ltree_pickling()
        pool_size = self._config.find('pool-size')
        self._pool_size = int(
            pool_size.text) if pool_size is not None else DEFAULT_POOL_SIZE
        self._pool = Pool(
            self._pool_size, initializer=_init_worker,
            initargs=(self._args.progress_bar or self._args.quiet,))

    def run(self):
        data = self._fetch_data()
        tv, programs = self._build_xmltv(data)
        # Write a valid file before the best-effort enrichment pass so a
        # crash or kill during metadata lookups doesn't lose the scrape.
        self._write_xmltv(tv)
        if self._metadata:
            try:
                self._apply_metadata(programs)
            except Exception as e:
                self._log.error(f'Metadata pass failed: {e}')
            self._write_xmltv(tv)
        if isinstance(self._proxy, ProxyPool):
            stats = self._proxy.stats()
            self._log.info(
                f"Proxy pool: {stats['alive']}/{stats['total']} proxies "
                'alive at end of run (parent process stats; workers '
                'maintain their own pools)')

    def _build_xmltv(self, data: Dict[ChannelKey, List[Programme]]):
        channels = []
        programs = []
        for chan_key, prgs in sorted(data.items(),
                                     key=lambda item: item[0].id):
            if chan_key.channel is not None:
                channels.append(chan_key.channel)
            programs.extend(
                sorted(prgs, key=lambda prg: prg and prg.start))
        programs = self._dedupe_programs(programs)
        self._post_process_programs(programs)
        return Tv(channels, programs,
                  date=date.today().strftime('%Y%m%d%H%M%S'),
                  generator_info_name='py_epg'), programs

    def _apply_metadata(self, programs: List[Programme]):
        """
        Batched metadata resolution: workers tag programmes with
        '_meta_lookup' params instead of doing inline HTTP lookups, so
        identical lookups are deduplicated across all channels/airings.
        The unique lookups are resolved on the same worker pool as the
        fetch - they inherit each worker's request throttling and, with
        a proxy list, the rotating proxy session (per-request proxy
        switch + retry through the next proxy).
        """
        if not self._metadata:
            return
        by_key = defaultdict(list)
        for p in programs:
            args = getattr(p, '_meta_lookup', None)
            if args is not None:
                by_key[args].append(p)
        if not by_key:
            return
        self._log.info(
            f'Resolving metadata for {len(by_key)} unique programmes '
            f'({len(programs)} total) using {self._pool_size} workers.')
        metas = {}
        done = 0
        last_log = time.monotonic()
        results = self._pool.imap_unordered(
            self._resolve_metadata, by_key.keys(), chunksize=1)
        for args, meta, error in tqdm.tqdm(
                results, total=len(by_key),
                disable=not self._args.progress_bar,
                desc='Metadata', dynamic_ncols=True):
            done += 1
            if error is not None:
                # a failed lookup is not cached - retried next run
                self._log.warning(
                    f'Metadata lookup failed for {args[0]!r}: {error}')
                continue
            metas[args] = meta
            # without progress bars, log a heartbeat so long
            # cold-cache passes don't look stuck
            if not self._args.progress_bar and \
                    time.monotonic() - last_log >= 10:
                last_log = time.monotonic()
                self._log.info(
                    f'Metadata: {done}/{len(by_key)} lookups resolved.')
        enriched = 0
        no_match = 0
        failed = 0
        by_provider = defaultdict(int)
        fields = defaultdict(int)
        for args, progs in by_key.items():
            if args not in metas:
                # lookup raised - not cached, retried next run
                failed += len(progs)
                continue
            meta = metas[args]
            if not meta:
                no_match += len(progs)
                continue
            by_provider[meta.get('_src', '?')] += len(progs)
            for p in progs:
                enriched += self._apply_program_metadata(p, meta, fields)
        provider_stats = ', '.join(
            f'{k}={v}' for k, v in sorted(by_provider.items()))
        field_stats = ', '.join(
            f'{k}={v}' for k, v in sorted(fields.items()))
        failed_stats = f', {failed} failed (retried next run)' \
            if failed else ''
        self._log.info(
            f'Metadata applied to {enriched} programmes '
            f'({provider_stats or "none"}), no match for {no_match}'
            f'{failed_stats}. Fields filled: {field_stats or "none"}.')

    def _resolve_metadata(self, args):
        """
        Pool task: resolves one unique '_meta_lookup' tuple inside a
        fetch worker. Errors come back as strings rather than being
        raised through the pool, so one bad lookup can't abort the
        whole enrichment pass.
        """
        try:
            return args, self._metadata.metadata_for(*args), None
        except Exception as e:
            return args, None, f'{type(e).__name__}: {e}'

    @staticmethod
    def _apply_program_metadata(p: Programme, meta: dict,
                                fields: dict = None) -> int:
        """
        Applies provider fields to a programme: artwork always wins
        (explicitly preferred), other fields fill gaps only - the EPG
        source stays authoritative for broadcast-specific data.
        """
        applied = []
        if meta.get('icon'):
            p.icon = [Icon(src=meta['icon'])]
            applied.append('icon')
        if meta.get('orig_title'):
            existing = {t.content[0] for t in p.title if t.content}
            if meta['orig_title'] not in existing:
                p.title.append(Title(content=[meta['orig_title']],
                                     lang='en'))
                applied.append('orig_title')
        if meta.get('episode_title') and not p.sub_title:
            p.sub_title.append(SubTitle(content=[meta['episode_title']]))
            applied.append('episode_title')
        if meta.get('desc') and not p.desc:
            p.desc.append(Desc(content=[meta['desc']]))
            applied.append('desc')
        if meta.get('year') and not p.date:
            p.date = meta['year']
            applied.append('year')
        if meta.get('countries') and not p.country:
            p.country = [Country(content=[c]) for c in meta['countries']]
            applied.append('countries')
        if meta.get('genres') and not p.category:
            p.category = [Category(content=[g]) for g in meta['genres']]
            applied.append('genres')
        if meta.get('rating'):
            p.rating.append(Rating(value=meta['rating'],
                                   system=meta.get('rating_system',
                                                  'metadata')))
            applied.append('rating')
        if fields is not None:
            for f in applied:
                fields[f] += 1
        return 1 if applied else 0

    def _dedupe_programs(self, programs: List[Programme]) -> List[Programme]:
        """
        Drops exact duplicate airings (same channel + timeslot). EPG
        sources occasionally emit the same programme several times in
        one listing; duplicates produce zero-length programmes after
        stop synthesis and inflate the output.
        """
        seen = set()
        result = []
        for p in programs:
            key = (p.channel, p.start, p.stop)
            if key in seen:
                continue
            seen.add(key)
            result.append(p)
        dropped = len(programs) - len(result)
        if dropped:
            self._log.info(
                f'Dropped {dropped} duplicate programme entries.')
        return result

    def _post_process_programs(self, programs: List[Programme]):
        for i, program in enumerate(programs):
            # Keep real stop times when the source provides them
            # (port.hu); otherwise synthesize from the next start time.
            if program.stop:
                continue
            if i < len(programs) - 1 and programs[i + 1].channel == program.channel:
                program.stop = programs[i + 1].start
            else:
                program.stop = f'{program.start[:8]}235959{program.start[14:]}'

    def _write_xmltv(self, tv: Tv):
        xmltv_out_file = pathlib.Path(self._config.find('filename').text)
        self._log.info(f'Writing results to {xmltv_out_file}..')
        write_file_from_xml(xmltv_out_file, tv)

    def _fetch_data(self) -> Dict[ChannelKey, List[Programme]]:
        pbar_id = 'All Channels'
        programs_by_channel = defaultdict(list)
        channels = self._config.findall('channel')
        missing_sites = {chan.attrib['site'] for chan in channels
                         if chan.attrib['site'] not in self._epg_scrapers}
        if missing_sites:
            raise RuntimeError(
                f'Could not find scraper(s) for site(s): {", ".join(sorted(missing_sites))}')
        self._log.info(
            f'Start grabbing programs for {len(channels)} channels using {self._pool_size} workers.')
        bar_unit_format = 'Progs: 0'
        bar_format = "{l_bar} {bar}|Chan: {n_fmt:>3}/{total_fmt:<3} [T:{elapsed} ETA:{remaining:<5}]"
        channel_programs = tqdm.tqdm(self._pool.imap_unordered(self._fetch_channel, channels, chunksize=1),
                                     total=len(channels),
                                     disable=not self._args.progress_bar,
                                     position=0,
                                     dynamic_ncols=True,
                                     colour='cyan',
                                     bar_format=bar_format,
                                     postfix={'T': len(programs_by_channel)},
                                     desc=f'{pbar_id: >{PBAR_NAME_COL_WIDTH}}')
        for i, (chan_key, chan_progs) in enumerate(channel_programs, 1):
            programs_by_channel[chan_key].extend(chan_progs)
            self._log.info(
                f'{chan_key.id}: {len(programs_by_channel[chan_key])} '
                f'programs successfully grabbed. '
                f'[{i}/{len(channels)} channels]')
        prog_count = sum([len(listElem)
                         for listElem in programs_by_channel.values()])
        self._log.info(
            f'Grabbing completed! A total of {prog_count} programs fetched.')
        return programs_by_channel

    def _fetch_channel(self, chan) -> Tuple[ChannelKey, List[Programme]]:
        site = chan.attrib['site']
        chan_site_id = chan.attrib['site_id']
        chan_xmltv_id = chan.attrib['xmltv_id']
        chan_name = chan.text

        scraper = self._epg_scrapers.get(site)
        if not scraper:
            raise RuntimeError(f'Could not find scraper for site={site}.')

        try:
            channel = scraper.fetch_channel(chan_site_id, chan_name)
        except requests.RequestException as e:
            # A single broken/missing channel shouldn't abort the whole run.
            self._log.error(
                f'{chan_site_id}: failed to fetch channel: {e}')
            return ChannelKey(chan_site_id.upper(), None), []
        key = ChannelKey(channel.id, channel)
        today = scraper.today()
        days = int(self._config.find('timespan').text)

        programs = []

        process = current_process()
        pbar_id = chan_site_id if len(chan_site_id) <= PBAR_NAME_COL_WIDTH - 2 \
            else chan_site_id[:PBAR_NAME_COL_WIDTH - 2] + '..'
        bar_format = "{l_bar} {bar}|Days: {n_fmt:>3}/{total_fmt:<3} [T:{elapsed} ETA:{remaining:<5}]"
        days_range = tqdm.tqdm(iterable=range(days),
                               disable=not self._args.progress_bar,
                               position=process._identity[0],
                               unit='day',
                               leave=False,
                               dynamic_ncols=True,
                               bar_format=bar_format,
                               colour='green',
                               desc=f'{pbar_id: >{PBAR_NAME_COL_WIDTH}}')
        for i in days_range:
            fetch_date = today + timedelta(days=i)
            try:
                day_programs = scraper.fetch_programs(
                    channel, chan_site_id, fetch_date)
            except requests.RequestException as e:
                self._log.error(
                    f'{chan_site_id}: failed to fetch programs for '
                    f'{fetch_date}: {e}')
                continue
            programs.extend(day_programs)
            self._log.debug(
                f'{chan_site_id}: grabbed {len(day_programs)} programs for date {fetch_date}.')
        return key, programs

    def _build_proxy(self):
        """
        Returns a ProxyPool when <proxy-list> is configured, a proxy URL
        string for a single static <proxy>, or None.
        """
        pool_cfg = self._config.find('proxy-list')
        if pool_cfg is not None:
            pool = ProxyPool(
                url=pool_cfg.attrib.get('url'),
                proxies=[p.text.strip()
                         for p in pool_cfg.findall('proxy') if p.text],
                refresh=int(pool_cfg.attrib.get('refresh', 300)),
                max_fails=int(pool_cfg.attrib.get('max-fails', 3)),
                cooldown=int(pool_cfg.attrib.get('cooldown', 600)),
                timeout=int(pool_cfg.attrib.get('timeout', 10)),
                tries=int(pool_cfg.attrib.get('tries', 3)),
                allow_direct=argparse_str2bool(
                    pool_cfg.attrib.get('allow-direct', 'true')),
                stats_db=self._cache)
            self._log.info(
                f'Proxy pool configured (url={pool.url}, '
                f'{len(pool._proxies)} static proxies)')
            return pool
        proxy = self._config.find('proxy')
        return proxy.text if proxy is not None else None

    def _build_cache(self) -> Cache:
        cfg = self._config.find('cache')
        if cfg is None or not argparse_str2bool(
                cfg.attrib.get('enabled', 'true')):
            return Cache(enabled=False)
        return Cache(
            path=cfg.attrib.get('file', 'epg_cache.sqlite'),
            ttls={
                'channel': int(cfg.attrib.get('channel-ttl', 604800)),
                'program': int(cfg.attrib.get('program-ttl', 2592000)),
                'meta': int(cfg.attrib.get('meta-ttl', 7776000)),
                'listing': int(cfg.attrib.get('listing-ttl', 21600)),
            })

    def _build_metadata(self):
        cfg = self._config.find('metadata')
        if cfg is not None and 'workers' in cfg.attrib:
            self._log.warning(
                "<metadata> 'workers' attribute is ignored - lookups "
                "now run on the fetch worker pool (see <pool-size>)")
        # Browser UA - the default 'python-requests' UA gets flagged by
        # bot protection before rate limits even apply.
        ua_cfg = self._config.find('user-agent')
        ua = ua_cfg.text if ua_cfg is not None else UA.random
        # A configured proxy pool also spreads lookups across egress IPs -
        # bans are per-IP, so rotation is the only safe way to raise the
        # request rate.
        # lookups obey the same <request-delay> as the scrapers - the
        # session itself throttles, so every provider request is covered
        request_delay = self._request_delay()
        if isinstance(self._proxy, ProxyPool):
            session = get_proxy_session(
                pool=self._proxy, user_agent=ua,
                request_delay=request_delay)
        else:
            # Fast-fail retry policy: unlike scraping (which retries
            # through rate-limit bans), a throttled lookup should error
            # out quickly. Failures are best-effort and never cached.
            session = get_http_session(
                proxy=self._proxy, user_agent=ua,
                retries=1, backoff_factor=0.5, retry_after_max=10,
                request_delay=request_delay)
        return build_metadata(cfg, session=session, cache=self._cache)

    def _request_delay(self) -> float:
        delay_cfg = self._config.find('request-delay')
        return float(delay_cfg.text) \
            if delay_cfg is not None and delay_cfg.text else 0.0

    def _init_epg_scrapers(self) -> Dict[str, EpgScraper]:
        result = {}
        implementations = EpgScraper.__subclasses__()
        user_agent = self._config.find('user-agent')
        request_delay = self._request_delay()
        for scraper_class in implementations:
            obj = scraper_class(proxy=self._proxy,
                                user_agent=user_agent.text if user_agent is not None else None,
                                cache=self._cache,
                                metadata=self._metadata,
                                request_delay=request_delay)
            result[obj.site_name()] = obj
        return result

    def _read_config(self) -> Dict:
        return ET.parse(self._args.config)

    def _parse_args(self):
        # Initialize parser
        parser = argparse.ArgumentParser(
            prog='py_epg',
            description='A simple, multi-threaded, modular EPG grabber written in Python')
        parser.add_argument(
            "-p", "--progress-bar", help="Show progress bars. Default: False",
            default=False, type=argparse_str2bool, nargs='?', const=True)
        parser.add_argument(
            "-q", "--quiet", help="Quiet mode (no progress-bar, no console logs). Default: False",
            default=False, type=argparse_str2bool, nargs='?', const=True)
        requiredArgs = parser.add_argument_group('required arguments')
        requiredArgs.add_argument(
            "-c", "--config", help="Path to py_epg.xml file", required=True)
        args = parser.parse_args()

        if args.quiet:
            args.progress_bar = False
        if args.progress_bar or args.quiet:
            # Disable console logging if progress-bar is enabled
            _remove_console_handler(logging.getLogger())
        return args

    def __getstate__(self):
        self_dict = self.__dict__.copy()
        del self_dict['_pool']
        return self_dict

    def __setstate__(self, state):
        self.__dict__.update(state)


def main(args=None):
    py_epg = PyEPG()
    py_epg.run()
