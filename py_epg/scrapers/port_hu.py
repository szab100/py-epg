#!/usr/bin/env python3
import json
import re
from datetime import date, datetime
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from py_epg.common.epg_scraper import EpgScraper
from xmltv.models import (Actor, Category, Channel, Country, Credits, Desc,
                          DisplayName, EpisodeNum, Icon, Length, LengthUnits,
                          PreviouslyShown, Programme, Rating, SubTitle, Title)


class PortHu(EpgScraper):
    """
    Scraper for port.hu's TV guide JSON API.

    Channel list:   GET /tvapi/init-new  (channel ids: 'tvchannel-N')
    Day schedule:   GET /tvapi?channel_id[]={id}&date=YYYY-MM-DD
    Detail pages:   event['film_url'] -> schema.org JSON-LD Movie block
                    cached under the canonical film id (movie-/episode-N),
                    so repeated airings of the same content reuse the
                    fetched details for free.
    """

    BASE = 'https://port.hu'
    API = f'{BASE}/tvapi'
    REQUEST_DELAY = 0.4   # default per-worker delay when no
    # <request-delay> is configured

    RE_SEASON_EP = re.compile(r'([IVXLCDM]+)\s*/\s*(\d+)\.\s*rész')
    RE_EPISODE = re.compile(r'\b(\d+)\.\s*rész')
    RE_YEAR = re.compile(r'(\d{4})')
    RE_LDJSON = re.compile(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.S)
    RE_DURATION = re.compile(r'PT(?:(\d+)H)?(\d+)M')
    ROMAN = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}

    def __init__(self, proxy=None, user_agent=None, cache=None,
                 metadata=None, request_delay=None):
        super().__init__(name=__name__, proxy=proxy,
                         user_agent=user_agent, cache=cache,
                         metadata=metadata,
                         request_delay=request_delay
                         if request_delay is not None
                         else self.REQUEST_DELAY)
        self._site_id = 'port.hu'
        self._tz_local = tz.gettz('Europe/Budapest')
        self._bootstrapped = False

    def site_name(self) -> str:
        return self._site_id

    def today(self) -> date:
        return datetime.now(tz=self._tz_local).date()

    # -- HTTP helpers ------------------------------------------------------

    def _bootstrap(self):
        # port.hu gates requests behind a session cookie set by the
        # token redirect on the landing page.
        if not self._bootstrapped:
            self._throttle()
            self._http.get(self.BASE, timeout=self._timeout)
            self._bootstrapped = True

    def _get_json(self, url, params=None) -> dict:
        self._bootstrap()
        self._throttle()
        resp = self._http.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, url) -> str:
        self._bootstrap()
        self._throttle()
        resp = self._http.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.text

    # -- channels ----------------------------------------------------------

    def _channel_map(self) -> dict:
        cache_key = f'channels:{self._site_id}'
        cmap = self._cache.get(cache_key)
        if cmap is None:
            data = self._get_json(f'{self.API}/init-new')
            cmap = {c['id']: {'name': c['name'], 'logo': c.get('logo')}
                    for c in data.get('channels', [])}
            self._cache.set(cache_key, cmap, 'channel')
        return cmap

    def fetch_channel(self, chan_site_id, xmltv_id, name) -> Channel:
        cache_key = f'channel:{self._site_id}:{chan_site_id}'
        cached = self._cache.get(cache_key)
        if cached is None:
            entry = self._channel_map().get(chan_site_id)
            if entry is None:
                raise requests.RequestException(
                    f'unknown port.hu channel id: {chan_site_id}')
            cached = entry
            self._cache.set(cache_key, cached, 'channel')
        return Channel(
            id=xmltv_id or f'{chan_site_id}.{self._site_id}'.upper(),
            display_name=[DisplayName(content=[name])],
            icon=Icon(src=cached['logo']) if cached.get('logo') else None)

    # -- programs ----------------------------------------------------------

    def fetch_programs(self, channel: Channel, channel_site_id: str,
                       fetch_date: date) -> List[Programme]:
        cache_key = (f'listing:{self._site_id}:{channel_site_id}:'
                     f'{fetch_date.strftime("%Y-%m-%d")}')
        entries = self._cache.get(cache_key)
        if entries is None:
            data = self._get_json(self.API, params={
                'channel_id[]': channel_site_id,
                'date': fetch_date.strftime('%Y-%m-%d')})
            events = (data.get('channels') or [{}])[0].get('programs') or []
            entries = [{
                'id': e['id'],
                'start': e.get('start_datetime'),
                'stop': e.get('end_datetime'),
                'title': e.get('title'),
                'episode_title': e.get('episode_title'),
                'short_description': e.get('short_description'),
                'is_repeat': bool(e.get('is_repeat')),
                'age_limit': (e.get('restriction') or {}).get('age_limit'),
                'category': (e.get('restriction') or {}).get('category'),
                'film_url': e.get('film_url'),
                'content_id': e.get('film_id')
                or (e.get('film_url') or '').rsplit('/', 1)[-1]
                or None,
            } for e in events if e.get('title') and e.get('start_datetime')]
            self._cache.set(cache_key, entries, 'listing')

        programs = []
        missing_details = 0
        for e in entries:
            start = self._xmltv_time(e['start'])
            program = Programme(
                channel=channel.id,
                start=start,
                stop=self._xmltv_time(e['stop']) if e.get('stop') else None,
                title=[Title(content=[e['title']], lang='hu')])
            details = self._get_program_details(e['content_id'],
                                                e['film_url']) \
                if e.get('content_id') else {}
            if not details:
                missing_details += 1
            self._apply_program_details(program, e, details)
            programs.append(program)
        if missing_details:
            self._log.debug(
                f'{channel_site_id}: no detail page for '
                f'{missing_details}/{len(programs)} programs on {fetch_date}')
        return programs

    @staticmethod
    def _xmltv_time(iso) -> Optional[str]:
        if not iso:
            return None
        return datetime.fromisoformat(iso).strftime('%Y%m%d%H%M%S %z')

    def _get_program_details(self, content_id, film_url) -> dict:
        if not film_url:
            return {}
        cache_key = f'program:{self._site_id}:{content_id}'
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            url = film_url if film_url.startswith('http') \
                else f'{self.BASE}{film_url}'
            html = self._get_text(url)
        except requests.RequestException as e:
            self._log.warning(
                f'Failed to fetch program details {film_url}: {e}')
            return {}
        details = self._parse_detail_page(html)
        self._cache.set(cache_key, details, 'program')
        return details

    def _parse_detail_page(self, html) -> dict:
        """Extracts the schema.org JSON-LD block from a film page."""
        for m in self.RE_LDJSON.finditer(html):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if not isinstance(data, dict) or 'description' not in data:
                continue
            persons = lambda key: [
                p['name'] for p in data.get(key) or [] if p.get('name')]
            countries = [c['name'] for c in data.get('countryOfOrigin') or []
                         if c.get('name')]
            duration = None
            dm = self.RE_DURATION.search(data.get('duration') or '')
            if dm:
                duration = str(int(dm.group(1) or 0) * 60 + int(dm.group(2)))
            rating = data.get('aggregateRating') or {}
            image = data.get('image') or {}
            return {
                'desc': data.get('description') or None,
                'genre': data.get('genre') or None,
                'year': data.get('copyrightYear')
                or self._year_from(data.get('datePublished')),
                'countries': countries or None,
                'directors': persons('director'),
                'actors': persons('actor'),
                'length_min': duration,
                'rating': f"{rating['ratingValue']}/10"
                if rating.get('ratingValue') else None,
                'rating_system': 'port.hu-score',
                'age_rating': str(data['contentRating'])
                if data.get('contentRating') is not None else None,
                'icon': image.get('url'),
            }
        return {}

    def _year_from(self, value):
        m = self.RE_YEAR.search(str(value or ''))
        return m.group(1) if m else None

    def _season_episode(self, text):
        m = self.RE_SEASON_EP.search(text or '')
        if m:
            return self._roman_to_int(m.group(1)), int(m.group(2))
        m = self.RE_EPISODE.search(text or '')
        return (None, int(m.group(1))) if m else (None, None)

    def _roman_to_int(self, s) -> Optional[int]:
        total, prev = 0, 0
        for c in reversed(s):
            v = self.ROMAN.get(c)
            if v is None:
                return None
            total += -v if v < prev else v
            prev = max(prev, v)
        return total if total > 0 else None

    def _apply_program_details(self, program: Programme, e: dict,
                               details: dict):
        if self._metadata:
            season, episode = self._season_episode(
                e.get('short_description'))
            program._meta_lookup = (
                e['title'], None,
                details.get('year') or self._year_from(
                    e.get('short_description')),
                season, episode)
        if details.get('icon'):
            program.icon = [Icon(src=details['icon'])]
        if e.get('episode_title'):
            program.sub_title.append(
                SubTitle(content=[e['episode_title']], lang='hu'))
        if details.get('desc'):
            program.desc.append(Desc(content=[details['desc']], lang='hu'))
        if details and (details.get('directors')
                        or details.get('actors')):
            credits = Credits()
            if details.get('directors'):
                credits.director = details['directors']
            if details.get('actors'):
                credits.actor = [Actor(content=[a])
                                 for a in details['actors']]
            program.credits = credits
        year = details.get('year') or self._year_from(
            e.get('short_description'))
        if year:
            program.date = year
        genre = details.get('genre')
        cat = genre or e.get('category')
        if cat:
            program.category = [Category(content=[cat], lang='hu')]
        if details.get('countries'):
            program.country = [Country(content=[c])
                               for c in details['countries']]
        season, episode = self._season_episode(
            e.get('short_description'))
        if season or episode:
            xmltv_ns, onscreen = '', ''
            if season:
                xmltv_ns += f'{season - 1}.'
                onscreen += f'S{season:02d}'
            if episode:
                xmltv_ns += f'{episode - 1}.'
                onscreen += f'E{episode:02d}'
            program.episode_num = [
                EpisodeNum(content=[onscreen], system='onscreen'),
                EpisodeNum(content=[xmltv_ns], system='xmltv_ns')]
        if details.get('length_min'):
            program.length = Length(content=[details['length_min']],
                                    units=LengthUnits.MINUTES)
        if e.get('age_limit') is not None:
            program.rating.append(Rating(value=str(e['age_limit']),
                                         system='port.hu'))
        if details.get('rating'):
            program.rating.append(Rating(value=details['rating'],
                                         system=details.get(
                                             'rating_system', 'port.hu')))
        if e.get('is_repeat'):
            program.previously_shown = PreviouslyShown()
