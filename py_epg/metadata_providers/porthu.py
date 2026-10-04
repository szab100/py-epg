#!/usr/bin/env python3
"""port.hu-backed programme metadata provider."""

import logging
import random
import re
import threading
import time
from typing import Optional

import requests

from py_epg.common.metadata import MetadataProvider, norm_title

log = logging.getLogger(__name__)

# country/nationality words used in port.hu's search 'subtitle' field
# (e.g. 'amerikai-német sci-fi sorozat, 1987') - not exhaustive, but
# covers the common production countries on Hungarian TV
COUNTRIES = {
    'amerikai', 'magyar', 'brit', 'angol', 'skót', 'ír', 'walesi',
    'német', 'osztrák', 'svájci', 'francia', 'olasz', 'spanyol',
    'portugál', 'svéd', 'norvég', 'dán', 'finn', 'izlandi', 'holland',
    'belga', 'luxemburgi', 'lengyel', 'cseh', 'szlovák', 'szlovén',
    'horvát', 'szerb', 'román', 'bolgár', 'görög', 'török', 'orosz',
    'ukrán', 'észt', 'lett', 'litván', 'kanadai', 'ausztrál',
    'új-zélandi', 'japán', 'kínai', 'koreai', 'dél-koreai', 'indiai',
    'thai', 'vietnami', 'hongkongi', 'tajvani', 'mexikói', 'brazil',
    'argentín', 'chilei', 'kolumbiai', 'perui', 'kubai', 'dél-afrikai',
    'egyiptomi', 'izraeli', 'iráni', 'szaúdi', 'arab', 'nigériai',
    'kenyai', 'orosz-szovjet', 'szovjet', 'jugoszláv', 'ndks',
}
# format words - not genres
FORMAT_WORDS = {
    'sorozat', 'minisorozat', 'film', 'tv-film', 'tévéfilm', 'műsor',
    'dokumentumfilm', 'mozifilm', 'rövidfilm', 'televíziós', 'ismeretterjesztő',
}


class PortHuMetadata(MetadataProvider):
    """
    Uses port.hu's (undocumented) suggest-list endpoint.

    The site gates every request behind a session token: the first request
    is redirected through ?token=... and sets a cookie. requests.Session
    handles this transparently once the base page has been fetched.

    Only returns data when the Hungarian title matches exactly and the
    year (from 'subtitle') is within +-1 when known.

    Optional <metadata> attribute: porthu_delay=<seconds> - minimum delay
    between requests, shared across parallel lookup threads (default 0.25).
    """

    name = 'porthu'

    SEARCH_URL = 'https://port.hu/search/suggest-list'
    RE_YEAR = re.compile(r'(\d{4})')
    # class-level so they don't get pickled into worker processes
    _BOOTSTRAP_LOCK = threading.Lock()
    _RATE_LOCK = threading.Lock()
    _last_request = 0.0

    def __init__(self, cfg, session: requests.Session,
                 cache=None, timeout: int = 15):
        super().__init__(cfg, session, cache, timeout)
        self._bootstrapped = False
        # polite minimum delay between requests (seconds) - parallel
        # threads share the limiter so workers can't burst the site.
        self._delay = float(self.cfg_opt(cfg, 'delay', '0.25') or 0.25)

    def _cache_key(self, title, orig_title, year, season, episode):
        # port.hu has no per-episode artwork - same key for all episodes
        return f'meta:{self.name}:{norm_title(title)}:' \
               f'{norm_title(orig_title or "")}:{year or ""}'

    def _throttle(self):
        with self._RATE_LOCK:
            # jitter: a metronomic cadence is itself a bot signature
            gap = self._delay * random.uniform(1.0, 1.6)
            wait = gap - (time.monotonic()
                          - PortHuMetadata._last_request)
            if wait > 0:
                time.sleep(wait)
            PortHuMetadata._last_request = time.monotonic()

    def _bootstrap(self):
        # Acquires the session token cookie (redirect chain). Guarded so
        # parallel lookup threads can't race each other into unauthen-
        # ticated requests (which 302 and would poison the neg cache).
        if not self._bootstrapped:
            with self._BOOTSTRAP_LOCK:
                if not self._bootstrapped:
                    self._throttle()
                    self._http.get('https://port.hu/',
                                   timeout=self._timeout)
                    self._bootstrapped = True

    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[dict]:
        # RequestExceptions propagate: a transient failure must not be
        # stored as a negative cache entry - it's retried on next run.
        self._bootstrap()
        # port.hu search is Hungarian - query the localised title first.
        for q in (title, orig_title):
            if not q:
                continue
            self._throttle()
            r = self._http.get(self.SEARCH_URL, params={'q': q},
                               timeout=self._timeout)
            r.raise_for_status()
            try:
                results = r.json()
            except ValueError:
                continue
            match = self._best_match(results, title, orig_title, year)
            if match:
                return self._to_metadata(match)
        return None

    def _best_match(self, results, title, orig_title, year):
        norm_titles = {norm_title(t) for t in (title, orig_title) if t}
        for res in results:
            if norm_title(res.get('name', '')) not in norm_titles:
                continue
            if year:
                m = self.RE_YEAR.search(res.get('subtitle') or '')
                if not m or abs(int(m.group(1)) - int(year)) > 1:
                    continue
            return res
        return None

    def _to_metadata(self, res) -> dict:
        meta = {'title': res.get('name') or None}
        if res.get('thumbnail'):
            meta['icon'] = res['thumbnail']
        countries, genres, year = self._parse_subtitle(
            res.get('subtitle'))
        if year:
            meta['year'] = year
        if countries:
            meta['countries'] = countries
        if genres:
            meta['genres'] = genres
        return meta

    @staticmethod
    def _parse_subtitle(subtitle):
        """
        'amerikai-német sci-fi sorozat, 1987' ->
        (['amerikai', 'német'], ['sci-fi'], '1987')
        """
        if not subtitle:
            return [], [], None
        m = re.search(r'(\d{4})\s*$', subtitle)
        year = m.group(1) if m else None
        head = subtitle[:m.start()].rstrip(' ,') if m else subtitle
        countries, genres = [], []
        for tok in re.split(r'\s+', head):
            tok = tok.strip(',.')
            if not tok:
                continue
            parts = tok.lower().split('-')
            # 'amerikai-német' is a compound country, but 'sci-fi' is a
            # genre - only split into countries when every part is one
            if all(p in COUNTRIES for p in parts):
                countries.extend(parts)
            elif tok.lower() not in FORMAT_WORDS:
                genres.append(tok)
        return countries, genres, year
