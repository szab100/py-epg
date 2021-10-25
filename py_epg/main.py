#!/usr/bin/env python3

import argparse
import logging
import os
import pathlib
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from multiprocessing import Pool
from pprint import pprint
from typing import Dict, List, Set, Tuple

import requests
import xmltv
from dateutil.parser import parse
from lxml import etree as ET
from xmltv import xmltv_helpers
from xmltv.models import Channel, Programme, Tv

from py_epg.common.epg_scraper import EpgScraper
from py_epg.common.multiprocess_helper import setup_ltree_pickling
from py_epg.common.types import ChannelKey
from py_epg.scrapers import *

LOG = logging.getLogger(__name__)
DEFAULT_POOL_SIZE = 1


class PyEPG:
    """Main class of PyEPG"""

    def __init__(self):
        self._log = logging.getLogger(__name__)
        self._args = self._parse_args()
        self._config = self._read_config()
        self._epg_scrapers = self._init_epg_scrapers()
        setup_ltree_pickling()
        pool_size = self._config.find('pool-size')
        self._pool = Pool(int(pool_size.text)
                          if pool_size is not None else DEFAULT_POOL_SIZE)

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
        programs_by_channel = defaultdict(list)
        channels = self._config.findall('channel')
        channel_programs = self._pool.map(self._fetch_channel, channels)
        for i, (chan_key, chan_progs) in enumerate(channel_programs):
            programs_by_channel[chan_key].extend(chan_progs)
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
        for i in range(days):
            fetch_date = today + timedelta(days=i)
            day_programs = scraper.fetch_programs(
                channel, chan_site_id, fetch_date)
            key = ChannelKey(channel.id, channel)
            programs.extend(day_programs)
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
            description='A simple, multi-threaded, modular EPG grabber written in Python')
        # parser.add_argument(
        #     "-p", "--proxy", help="Proxy URL, eg http://1.2.3.4:3128")
        requiredArgs = parser.add_argument_group('required arguments')
        requiredArgs.add_argument(
            "-c", "--config", help="Path to py_epg.xml file", required=True)
        return parser.parse_args()

    def __getstate__(self):
        self_dict = self.__dict__.copy()
        del self_dict['_pool']
        return self_dict

    def __setstate__(self, state):
        self.__dict__.update(state)


def main(args=None):
    py_epg = PyEPG()
    py_epg.run()
