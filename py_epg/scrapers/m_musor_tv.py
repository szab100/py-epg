#!/usr/bin/env python3
import logging
import re
from datetime import date
from pprint import pprint
from string import Template
from typing import List

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
    "((?=[MDCLXVI])M*D?C{0,4}L?X{0,4}V?I{0,4})\.\/([0-9]+)\.")
RE_EPISODE_RANGE = re.compile("([0-9]+)\.-([0-9]+)\.")
RE_SINGLE_EPISODE = re.compile("([0-9]+)\.")


class MusorTvMobile(EpgScraper):
    def __init__(self, proxy=None, user_agent=None):
        super().__init__(name=__name__, proxy=proxy, user_agent=user_agent)
        self._site_id = "m.musor.tv"
        self._base_url = 'https://m.musor.tv'
        self._page_encoding = 'utf-8'
        self._chan_id_tpl = Template('$chan_id.' + self._site_id)
        self._day_url_tpl = Template(
            self._base_url + '/napi/tvmusor/$chan_site_id/$date')
        self._tz_utc = tz.tzutc()
        self._tz_local = tz.tzlocal()

    def site_name(self) -> str:
        return self._site_id

    def fetch_channel(self, chan_site_id, name) -> Channel:
        today_str = date.today().strftime("%Y.%m.%d")
        url = self._day_url_tpl.substitute(
            chan_site_id=chan_site_id, date=today_str)
        soup = self._get_soup(url)
        channel_id = self._chan_id_tpl.substitute(chan_id=chan_site_id).upper()
        channel_logo = soup.select_one('img.channelheaderlink')
        channel_logo_src = self._base_url + \
            channel_logo.attrs['src'] if channel_logo else None
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
            program = self._get_program(channel_site_id, fetch_date, prg)
            if program:
                programs.append(program)
        return programs

    def _get_program(self, chan_site_id: str, fetch_date: date, prg: BeautifulSoup) -> Programme:
        # 1. Fetch basic program info from the daily listing page
        channel_id = self._chan_id_tpl.substitute(chan_id=chan_site_id).upper()
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

        # 2. Fetch extended program info from program details page
        prg_details_link = prg.select_one(
            'h3.wideprogentry_progtitle > a').attrs['href']
        prg_details_page = self._get_soup(self._base_url + prg_details_link)

        self._set_prg_icon(program, prg_details_page)
        self._set_prg_fields_from_mixed_description(program, prg_details_page)

        self._log.trace(
            f'New program CH: {channel_id} ENC: {prg.original_encoding} P: {prg_title}')
        return program

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
            stripped_title = title.rsplit(' ', 1)[0]
            program.title = [Title(content=[stripped_title], lang='hu')]

    def _set_prg_sub_title_and_year(self, program, prg):
        prg_sub_title = prg.select_one('div[itemprop="description"]')
        if prg_sub_title:
            subtitle = prg_sub_title.get_text(strip=True)
            parts = subtitle.split(',')
            if len(parts) > 1:
                year = parts[-1]
                if '-' in year:
                    # 2005-2010 => pick end year, eg. 2010
                    year = year.split('-')[-1]
                program.date = [year]
                subtitle = SubTitle(content=[','.join(parts[:-1])], lang='hu')
                program.sub_title = [subtitle] + program.sub_title
            else:
                # edge case: no subtitle, just a year
                if subtitle.isnumeric():
                    program.date = [subtitle]
                else:
                    program.sub_title = [subtitle]

    def _set_prg_icon(self, program, prg_details_page):
        prg_icon = prg_details_page.select_one('img[itemprop="image"]')
        if prg_icon:
            program.icon = [Icon(src=self._base_url + prg_icon.attrs['src'])]

    def _set_prg_fields_from_mixed_description(self, program, prg_details_page):
        prg_mixed_desc = clean_text(
            prg_details_page.select_one('div.eventinfolongdescinner')).strip()

        if prg_mixed_desc:
            if len(prg_mixed_desc.splitlines()) == 1:
                program.desc.append(Desc(content=[prg_mixed_desc]))
                return

            # pprint(prg_mixed_desc)
            result = RE_MIXED_DESCRIPTION.search(prg_mixed_desc)
            if result and len(result.groups()):
                # pprint(result.groups())
                # title in orig lang
                if result.group(1):
                    program.title.append(
                        Title(content=[result.group(1)], lang='en'))

                if result.group(2):
                    content = result.group(2).strip()
                    parts = content.split('\n\n')
                    if len(parts) >= 2:
                        # sub-title + description
                        program.sub_title.append(
                            SubTitle(content=[parts[0].strip()]))
                        desc = '\n'.join(parts[1:]).strip()
                        if desc:
                            program.desc.append(Desc(content=[desc]))
                    elif content:
                        # description only
                        program.desc.append(Desc(content=[content]))
                # director, cast
                if result.group(3) or result.group(4):
                    separator = re.compile('[,;]+ ')
                    credits = Credits()
                    if result.group(3):
                        credits.director = separator.split(
                            result.group(3).strip())
                    if result.group(4):
                        actors = separator.split(result.group(4).strip())
                        credits.actor = [Actor(content=[actor])
                                         for actor in actors]
                    program.credits = credits

    def _get_soup(self, url) -> BeautifulSoup:
        page = self._http.get(url)
        return BeautifulSoup(page.text, "html.parser")
