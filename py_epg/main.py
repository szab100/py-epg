#!/usr/bin/env python3

import argparse
import logging
import os
import pathlib
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from multiprocessing import Pool, current_process
from pprint import pprint
from typing import Dict, List, Set, Tuple

import requests
import tqdm
import xmltv
from dateutil.parser import parse
from lxml import etree as ET
from xmltv import xmltv_helpers
from xmltv.models import Channel, Programme, Tv

from py_epg.common.epg_scraper import EpgScraper
from py_epg.common.multiprocess_helper import setup_ltree_pickling
from py_epg.common.types import ChannelKey
from py_epg.common.utils import argparse_str2bool
from py_epg.scrapers import *

DEFAULT_POOL_SIZE = 1
PBAR_NAME_COL_WIDTH = 15


class PyEPG:
    """Main class of PyEPG"""

    def __init__(self):
        self._args = self._parse_args()
        self._log = logging.getLogger(__name__)
        self._config = self._read_config()
        self._epg_scrapers = self._init_epg_scrapers()
        setup_ltree_pickling()
        pool_size = self._config.find('pool-size')
        self._pool_size = int(
            pool_size.text) if pool_size is not None else DEFAULT_POOL_SIZE
        self._pool = Pool(self._pool_size)

    def run(self):
        data = self._fetch_data()
        tv = self._build_xmltv(data)
        self._write_xmltv(tv)

    def _build_xmltv(self, data: Dict[ChannelKey, List[Programme]]):
        channels = []
        programs = []
        for chan_key, prgs in sorted(data.items(),
                                     key=lambda item: item[0].id):
            channels.append(chan_key.channel)
            programs.extend(
                sorted(prgs, key=lambda prg: prg and prg.start))
        self._post_process_programs(programs)
        return Tv(channels, programs, date=date.today().strftime('%Y%m%d%H%M%S'), generator_info_name='py_epg')

    def _post_process_programs(self, programs: List[Programme]):
        for i, program in enumerate(programs):
            # Set stop times
            if i < len(programs) - 1 and programs[i + 1].channel == program.channel:
                program.stop = programs[i + 1].start
            else:
                program.stop = f'{program.start[:8]}235959{program.start[14:]}'

    def _write_xmltv(self, tv: Tv):
        xmltv_out_file = pathlib.Path(self._config.find('filename').text)
        self._log.info(f'Writing results to {xmltv_out_file}..')
        xmltv_helpers.write_file_from_xml(xmltv_out_file, tv)

    def _fetch_data(self) -> Dict[ChannelKey, List[Programme]]:
        pbar_id = 'All Channels'
        programs_by_channel = defaultdict(list)
        channels = self._config.findall('channel')
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
        for (chan_key, chan_progs) in channel_programs:
            programs_by_channel[chan_key].extend(chan_progs)
            self._log.info(
                f'{chan_key.id}: {len(programs_by_channel[chan_key])} programs successfully grabbed.')
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
            self._log.error(f'Could not find scraper for site={site}.')
            sys.exit(-1)

        channel = scraper.fetch_channel(chan_site_id, chan_name)
        today = date.today()
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
            day_programs = scraper.fetch_programs(
                channel, chan_site_id, fetch_date)
            key = ChannelKey(channel.id, channel)
            programs.extend(day_programs)
            self._log.debug(
                f'{chan_site_id}: grabbed {len(day_programs)} programs for date {fetch_date}.')
        return key, programs

    def _init_epg_scrapers(self) -> Dict[str, EpgScraper]:
        result = {}
        implementations = EpgScraper.__subclasses__()
        proxy = self._config.find('proxy')
        user_agent = self._config.find('user-agent')
        for scraper_class in implementations:
            obj = scraper_class(proxy=proxy.text if proxy is not None else None,
                                user_agent=user_agent.text if user_agent is not None else None)
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
            logging.getLogger().removeHandler(logging.getLogger().handlers[0])
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
