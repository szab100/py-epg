#!/usr/bin/env python3
import re
from datetime import date, datetime, time, timedelta
from string import Template
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from py_epg.common.epg_scraper import EpgScraper
from xmltv.models import (Actor, Category, Channel, Country, Credits, Desc,
                          DisplayName, EpisodeNum, Icon, Length, LengthUnits,
                          PreviouslyShown, Programme, Rating, SubTitle, Title)

RE_DETAILS_CALL = re.compile(
    r"loadDetailsVertical\(this,\s*'([^']+)',\s*(\d+),\s*'([^']+)'\)")
RE_NAME_SEPARATOR = re.compile(r'[,;]\s*')


class TvMustraHu(EpgScraper):
    """
    Scraper for www.tvmustra.hu.

    Listings are server-rendered per-channel/day pages:
        /tvmusor/{CHANNEL_ID}/{YYYY-MM-DD}
    Each programme's extended metadata is available as JSON:
        /tvmusor/index.php?ajax_action=get_details&table={table}&id={prog_id}
    (the 'table' token comes from the programme's onclick handler).
    """

    def __init__(self, proxy=None, user_agent=None, cache=None):
        super().__init__(name=__name__, proxy=proxy,
                         user_agent=user_agent, cache=cache)
        self._site_id = "tvmustra.hu"
        self._base_url = 'https://www.tvmustra.hu'
        self._chan_id_tpl = Template('$chan_id.' + self._site_id)
        self._tz_local = tz.gettz('Europe/Budapest')

    def site_name(self) -> str:
        return self._site_id

    def today(self) -> date:
        return datetime.now(tz=self._tz_local).date()

    def fetch_channel(self, chan_site_id, name) -> Channel:
        channel_id = self._chan_id_tpl.substitute(chan_id=chan_site_id).upper()
        cache_key = f'channel:{self._site_id}:{chan_site_id}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            channel_logo_src = cached['icon']
        else:
            soup = self._get_soup(f'{self._base_url}/tvmusor/{chan_site_id}')
            channel_logo = soup.select_one('div.ch-logo-white-bg img')
            channel_logo_src = self._abs_url(
                channel_logo.attrs['src']) if channel_logo else None
            self._cache.set(cache_key, {'icon': channel_logo_src}, 'channel')
        return Channel(
            id=channel_id,
            display_name=[DisplayName(content=[name])],
            icon=Icon(src=channel_logo_src) if channel_logo_src else None)

    def fetch_programs(self, channel: Channel, channel_site_id: str, fetch_date: date) -> List[Programme]:
        url = (f'{self._base_url}/tvmusor/{channel_site_id}/'
               f'{fetch_date.strftime("%Y-%m-%d")}')
        soup = self._get_soup(url)
        programs = []
        missing_details = 0
        # The daily page covers a broadcast day (early-morning programs roll
        # past midnight), so track the rollover and keep them all.
        last_time = None
        day_offset = 0
        for prg in soup.select('div.v-prog-container'):
            onclick = prg.attrs.get('onclick', '')
            m = RE_DETAILS_CALL.search(onclick)
            time_elem = prg.select_one('div.v-time')
            title_elem = prg.select_one('div.v-title')
            if not (m and time_elem and title_elem):
                continue
            table, prog_id, time_str = m.group(1), m.group(2), m.group(3)
            hour, minute = map(int, time_str.split(':'))
            if last_time and (hour, minute) < last_time:
                day_offset += 1
            last_time = (hour, minute)
            start_dt = datetime.combine(
                fetch_date + timedelta(days=day_offset),
                time(hour, minute, tzinfo=self._tz_local))
            program = Programme(
                channel=channel.id,
                start=start_dt.strftime('%Y%m%d%H%M%S %z'),
                title=[Title(content=[title_elem.get_text(strip=True)],
                             lang='hu')])
            details = self._get_program_details(table, prog_id)
            if not details:
                missing_details += 1
            self._apply_program_details(program, details)
            programs.append(program)
        if missing_details:
            self._log.info(
                f'{channel_site_id}: no detail data on the site for '
                f'{missing_details}/{len(programs)} programs on {fetch_date}')
        return programs

    def _get_program_details(self, table: str, prog_id: str) -> dict:
        cache_key = f'program:{self._site_id}:{table}:{prog_id}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        url = (f'{self._base_url}/tvmusor/index.php'
               f'?ajax_action=get_details&table={table}&id={prog_id}')
        try:
            payload = self._get_json(url)
        except requests.RequestException as e:
            # Details are best-effort: listing data (title, start) is kept.
            self._log.warning(
                f'Failed to fetch program details {url}: {e}')
            return {}
        if payload.get('status') != 'success':
            return {}
        details = self._parse_program_details(payload['data'])
        self._cache.set(cache_key, details, 'program')
        return details

    def _parse_program_details(self, d: dict) -> dict:
        def as_list(value):
            return [s.strip() for s in RE_NAME_SEPARATOR.split(value or '')
                    if s.strip()]

        images = d.get('kepek_list') or []
        season = d.get('evad') or ''
        episode = d.get('epizod') or ''
        # 'hossz' is minutes as a bare int on some channels, 'HH:MM:SS'
        # on others - normalise to whole minutes.
        length_min = None
        hossz = (d.get('hossz') or '').strip()
        if hossz.isdigit():
            length_min = hossz
        else:
            m = re.fullmatch(r'(\d+):(\d{2}):(\d{2})', hossz)
            if m:
                length_min = str(int(m.group(1)) * 60 + int(m.group(2))
                                 + (1 if int(m.group(3)) >= 30 else 0))
        return {
            # th_kepek = thumbnails; the same path under /kepek/ is full-size
            'icon': self._abs_url(images[0].replace('/th_kepek/', '/kepek/'))
            if images else None,
            'orig_title': d.get('angolcim') or None,
            'sub_titles': [d['alcim']] if d.get('alcim') else [],
            'descs': [d['tartalom']] if d.get('tartalom') else [],
            'directors': as_list(d.get('rendezo')),
            'actors': as_list(d.get('szereplok')),
            'date': d.get('gyartasiev') or None,
            'category': d.get('kategoria') or None,
            'country': d.get('gyartasio') or None,
            'season': int(season) if season.isdigit() else None,
            'episode': int(episode) if episode.isdigit() else None,
            'length_min': length_min,
            'age_rating': d.get('kor') or None,
            'previously_shown': bool(d.get('ismetles')),
        }

    def _apply_program_details(self, program: Programme, details: dict):
        if not details:
            return
        if details['icon']:
            program.icon = [Icon(src=details['icon'])]
        if details['orig_title']:
            program.title.append(
                Title(content=[details['orig_title']], lang='en'))
        for sub_title in details['sub_titles']:
            program.sub_title.append(SubTitle(content=[sub_title], lang='hu'))
        for desc in details['descs']:
            program.desc.append(Desc(content=[desc], lang='hu'))
        if details['directors'] or details['actors']:
            credits = Credits()
            if details['directors']:
                credits.director = details['directors']
            if details['actors']:
                credits.actor = [Actor(content=[a])
                                 for a in details['actors']]
            program.credits = credits
        if details['date']:
            program.date = details['date']
        if details['category']:
            program.category = [Category(content=[details['category']],
                                         lang='hu')]
        if details['country']:
            program.country = [Country(content=[details['country']])]
        if details['season'] or details['episode']:
            season, episode = details['season'], details['episode']
            xmltv_ns = ''
            onscreen = ''
            if season:
                xmltv_ns += f'{season - 1}.'
                onscreen += f'S{season:02d}'
            if episode:
                xmltv_ns += f'{episode - 1}.'
                onscreen += f'E{episode:02d}'
            program.episode_num = [
                EpisodeNum(content=[onscreen], system='onscreen'),
                EpisodeNum(content=[xmltv_ns], system='xmltv_ns')]
        if details['length_min']:
            program.length = Length(content=[details['length_min']],
                                    units=LengthUnits.MINUTES)
        if details['age_rating']:
            program.rating = [Rating(value=details['age_rating'],
                                     system='tvmustra.hu')]
        if details['previously_shown']:
            program.previously_shown = PreviouslyShown()

    def _abs_url(self, src: Optional[str]) -> Optional[str]:
        if not src:
            return None
        if src.startswith('/'):
            return self._base_url + src
        return src if src.startswith('http') else None

    def _get_soup(self, url) -> BeautifulSoup:
        page = self._http.get(url, timeout=self._timeout)
        page.raise_for_status()
        return BeautifulSoup(page.text, "html.parser")

    def _get_json(self, url) -> dict:
        page = self._http.get(url, timeout=self._timeout)
        page.raise_for_status()
        return page.json()
