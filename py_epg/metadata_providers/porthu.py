#!/usr/bin/env python3
"""port.hu-backed programme artwork provider."""

import logging
import re
from typing import Optional

import requests

from py_epg.common.metadata import MetadataProvider, norm_title

log = logging.getLogger(__name__)


class PortHuMetadata(MetadataProvider):
    """
    Uses port.hu's (undocumented) suggest-list endpoint.

    The site gates every request behind a session token: the first request
    is redirected through ?token=... and sets a cookie. requests.Session
    handles this transparently once the base page has been fetched.

    Only returns a poster thumbnail when the Hungarian title matches
    exactly and the year (from 'subtitle') is within +-1 when known.
    """

    name = 'porthu'

    SEARCH_URL = 'https://port.hu/search/suggest-list'
    RE_YEAR = re.compile(r'(\d{4})')

    def __init__(self, cfg, session: requests.Session,
                 cache=None, timeout: int = 15):
        super().__init__(cfg, session, cache, timeout)
        self._bootstrapped = False

    def _cache_key(self, title, orig_title, year, season, episode):
        # port.hu has no per-episode artwork - same key for all episodes
        return f'meta:{self.name}:{norm_title(title)}:' \
               f'{norm_title(orig_title or "")}:{year or ""}'

    def _bootstrap(self):
        if not self._bootstrapped:
            # Acquires the session token cookie (redirect chain).
            self._http.get('https://port.hu/', timeout=self._timeout)
            self._bootstrapped = True

    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[str]:
        try:
            self._bootstrap()
        except requests.RequestException as e:
            log.warning(f'port.hu session bootstrap failed: {e}')
            return None
        # port.hu search is Hungarian - query the localised title first.
        for q in (title, orig_title):
            if not q:
                continue
            try:
                r = self._http.get(self.SEARCH_URL, params={'q': q},
                                   timeout=self._timeout)
                r.raise_for_status()
            except requests.RequestException as e:
                log.warning(f'port.hu search failed for {q!r}: {e}')
                continue
            try:
                results = r.json()
            except ValueError:
                continue
            match = self._best_match(results, title, orig_title, year)
            if match and match.get('thumbnail'):
                return match['thumbnail']
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
