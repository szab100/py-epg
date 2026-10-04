#!/usr/bin/env python3
import logging
import re
from datetime import date, datetime
from pprint import pprint
from string import Template
from typing import List

import requests
import roman
from bs4 import BeautifulSoup
from dateutil import tz
from dateutil.parser import parse
from py_epg.common.epg_scraper import EpgScraper
from py_epg.common.utils import clean_text
from xmltv.models import (Actor, Channel, Credits, Desc, DisplayName,
                          EpisodeNum, Icon, Programme, SubTitle, Title)

RE_MIXED_DESCRIPTION = re.compile(
    # Group 1 (opt): (title in orig lang)
    r'^(?:\((.*)\)\n\n)'
    # Group 2 (opt): description
    r'?(?:(.*?)(?=Rendezte:|Rendező:|Főszereplők:|$))?'
    # Group 3 (opt): director
    r'(?:(?:Rendezte:|Rendező:)\s*(.*?)(?=Főszereplők:|$))?'
    # Group 4 (opt): cast
    r'(?:\s*Főszereplők:\s*(.*))?',
    flags=re.S
)

RE_SEASON_EPISODE = re.compile(
    r"((?=[MDCLXVI])M{0,4}(?:CM|CD|D?C{0,3})?(?:XC|XL|L?X{0,3})?"
    r"(?:IX|IV|V?I{0,3})?)\.\/([0-9]+)\.")
RE_EPISODE_RANGE = re.compile(r"([0-9]+)\.-([0-9]+)\.")
RE_SINGLE_EPISODE = re.compile(r"([0-9]+)\.")


class MusorTvMobile(EpgScraper):
    def __init__(self, proxy=None, user_agent=None, cache=None,
                 metadata=None, request_delay=None):
        super().__init__(name=__name__, proxy=proxy,
                         user_agent=user_agent, cache=cache,
                         metadata=metadata, request_delay=request_delay)
        self._site_id = "m.musor.tv"
        self._base_url = 'https://m.musor.tv'
        self._page_encoding = 'utf-8'
        self._chan_id_tpl = Template('$chan_id.' + self._site_id)
        self._day_url_tpl = Template(
            self._base_url + '/napi/tvmusor/$chan_site_id/$date')
        self._tz_utc = tz.tzutc()
        self._tz_local = tz.gettz('Europe/Budapest')

    def site_name(self) -> str:
        return self._site_id

    def today(self) -> date:
        return datetime.now(tz=self._tz_local).date()

    def fetch_channel(self, chan_site_id, xmltv_id, name) -> Channel:
        channel_id = xmltv_id or \
            self._chan_id_tpl.substitute(chan_id=chan_site_id).upper()
        cache_key = f'channel:{self._site_id}:{chan_site_id}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            channel_logo_src = cached['icon']
        else:
            today_str = self.today().strftime("%Y.%m.%d")
            url = self._day_url_tpl.substitute(
                chan_site_id=chan_site_id, date=today_str)
            soup = self._get_soup(url)
            channel_logo = soup.select_one('img.channelheaderlink')
            channel_logo_src = self._base_url + \
                channel_logo.attrs['src'] if channel_logo else None
            self._cache.set(cache_key, {'icon': channel_logo_src}, 'channel')
        return Channel(
            id=channel_id,
            display_name=[DisplayName(content=[name])],
            icon=Icon(src=channel_logo_src) if channel_logo_src else None)

    def fetch_programs(self, channel: Channel, channel_site_id: str, fetch_date: date) -> List[Programme]:
        date_str = fetch_date.strftime("%Y.%m.%d")
        channel_daily_progs_page = self._get_soup(self._day_url_tpl.substitute(
            chan_site_id=channel_site_id, date=date_str))
        programs_selector = 'section[itemscope]'
        programs = []
        for prg in channel_daily_progs_page.select(programs_selector):
            program = self._get_program(channel.id, fetch_date, prg)
            if program:
                programs.append(program)
        return programs

    def _get_program(self, channel_id: str, fetch_date: date, prg: BeautifulSoup) -> Programme:
        # 1. Fetch basic program info from the daily listing page
        prg_title = prg.select_one('[itemprop="name"]').get_text(strip=True)
        program = Programme(channel=channel_id,
                            title=[Title(content=[prg_title], lang='hu')],
                            clumpidx=None)

        self._set_prg_sub_title_and_year(program, prg)
        self._set_prg_episode_info(program, prg_title)
        prg_start = self._set_prg_start(program, prg)

        # skip program starting < 00:00 or > 23:59
        if prg_start.date() != fetch_date:
            return None

        # 2. Fetch extended program info from program details page (cached)
        prg_details_link = prg.select_one(
            'h3.wideprogentry_progtitle > a').attrs['href']
        details = self._get_program_details(self._base_url + prg_details_link)
        self._apply_program_details(program, details)

        self._log.trace(
            f'New program CH: {channel_id} ENC: {prg.original_encoding} P: {prg_title}')
        return program

    def _get_program_details(self, url) -> dict:
        cache_key = f'program:{url}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            prg_details_page = self._get_soup(url)
        except requests.RequestException as e:
            # Extended details are best-effort: a failed detail page still
            # leaves the program with its listing data (title, start, etc).
            self._log.warning(f'Failed to fetch program details {url}: {e}')
            return {'icon': None, 'orig_title': None, 'sub_titles': [],
                    'descs': [], 'directors': [], 'actors': []}
        details = self._parse_program_details(prg_details_page)
        self._cache.set(cache_key, details, 'program')
        return details

    def _parse_program_details(self, prg_details_page) -> dict:
        """Extracts cacheable fields from a program details page."""
        details = {'icon': None, 'orig_title': None, 'sub_titles': [],
                   'descs': [], 'directors': [], 'actors': []}
        prg_icon = prg_details_page.select_one('img[itemprop="image"]')
        if prg_icon:
            details['icon'] = self._base_url + prg_icon.attrs['src']

        desc_elem = prg_details_page.select_one('div.eventinfolongdescinner')
        if desc_elem is not None:
            self._parse_mixed_description(details, clean_text(desc_elem).strip())
        return details

    def _apply_program_details(self, program, details):
        if details['icon']:
            program.icon = [Icon(src=details['icon'])]
        if details['orig_title']:
            program.title.append(
                Title(content=[details['orig_title']], lang='en'))
        for sub_title in details['sub_titles']:
            program.sub_title.append(SubTitle(content=[sub_title]))
        for desc in details['descs']:
            program.desc.append(Desc(content=[desc]))
        if details['directors'] or details['actors']:
            credits = Credits()
            if details['directors']:
                credits.director = details['directors']
            if details['actors']:
                credits.actor = [Actor(content=[a])
                                 for a in details['actors']]
            program.credits = credits

    def _set_prg_start(self, program, prg):
        start = prg.select_one(
            'span[itemprop="startDate"]').attrs['content']
        start = parse(start.replace('GMT', 'T'))
        start = start.replace(tzinfo=self._tz_utc)
        start = start.astimezone(self._tz_local)
        program.start = start.strftime('%Y%m%d%H%M%S %z')
        return start

    def _set_prg_episode_info(self, program, title):
        # TV Shows - Season, Episode info in title
        m0 = RE_SEASON_EPISODE.search(title)
        # m1 = re_episode_range.search(title)
        m2 = RE_SINGLE_EPISODE.search(title)
        if m0 or m2:
            season = 0
            episode = 0
            if m0:
                season = roman.fromRoman(m0.group(1))
                episode = int(m0.group(2))
                title = title.split(str(m0.group()))[0].strip()
            if m2:
                episode = int(m2.group(1))
                title = title.split(str(m2.group()))[0].strip()
            onscreen = f'S{season:02d}E{episode:02d}' if season > 0 else f'S--E{episode:02d}'
            xmltv_ns = f'{season - 1}.{episode - 1}.' if season > 0 else f'.{episode - 1}.'
            program.episode_num = [EpisodeNum(content=[onscreen], system='onscreen'),
                                   EpisodeNum(content=[xmltv_ns], system='xmltv_ns')]
            # drop the dangling separator left behind by the marker
            # ('Cím - IV./12. rész' -> 'Cím') without eating title words
            stripped_title = title.rstrip(' -–—:')
            program.title = [Title(content=[stripped_title], lang='hu')]

    def _set_prg_sub_title_and_year(self, program, prg):
        prg_sub_title = prg.select_one('div[itemprop="description"]')
        if prg_sub_title:
            subtitle = prg_sub_title.get_text(strip=True)
            parts = subtitle.split(',')
            if len(parts) > 1:
                year = parts[-1].strip()
                if '-' in year:
                    # 2005-2010 => pick end year, eg. 2010
                    year = year.split('-')[-1]
                program.date = year
                subtitle = SubTitle(content=[','.join(parts[:-1])], lang='hu')
                program.sub_title = [subtitle] + program.sub_title
            else:
                # edge case: no subtitle, just a year
                if subtitle.isnumeric():
                    program.date = subtitle
                else:
                    program.sub_title = [SubTitle(content=[subtitle])]

    def _parse_mixed_description(self, details, prg_mixed_desc):
        if not prg_mixed_desc:
            return
        if len(prg_mixed_desc.splitlines()) == 1:
            details['descs'].append(prg_mixed_desc)
            return

        result = RE_MIXED_DESCRIPTION.search(prg_mixed_desc)
        if result and len(result.groups()):
            # title in orig lang
            if result.group(1):
                details['orig_title'] = result.group(1)

            if result.group(2):
                content = result.group(2).strip()
                parts = content.split('\n\n')
                if len(parts) >= 2:
                    # sub-title + description
                    details['sub_titles'].append(parts[0].strip())
                    desc = '\n'.join(parts[1:]).strip()
                    if desc:
                        details['descs'].append(desc)
                elif content:
                    # description only
                    details['descs'].append(content)
            # director, cast
            separator = re.compile('[,;]+ ')
            if result.group(3):
                details['directors'] = separator.split(
                    result.group(3).strip())
            if result.group(4):
                details['actors'] = separator.split(result.group(4).strip())

    def _get_soup(self, url) -> BeautifulSoup:
        self._throttle()
        page = self._http.get(url, timeout=self._timeout)
        page.raise_for_status()
        return BeautifulSoup(page.text, "html.parser")
