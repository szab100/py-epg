#!/usr/bin/env python3
"""TMDB-backed programme artwork provider."""

import logging
from typing import Optional

import requests

from py_epg.common.metadata import MetadataProvider, norm_title

log = logging.getLogger(__name__)

TMDB_API = 'https://api.themoviedb.org/3'
TMDB_IMG = 'https://image.tmdb.org/t/p/w500'


class TmdbMetadata(MetadataProvider):
    """
    Matching strategy (a 'proper match' is required - no fuzzy guesses):
      1. search/multi with the original (English) title when present,
         else the Hungarian title with language=hu-HU
      2. accept a hit only when the normalised title/original_title equals
         the query title, and the year (when known) is within +-1
      3. for TV hits with season+episode, prefer the episode still over
         the series poster

    Requires a free api-key attribute on <metadata> (themoviedb.org ->
    Settings -> API).
    """

    name = 'tmdb'

    def __init__(self, cfg, session: requests.Session,
                 cache=None, timeout: int = 15):
        super().__init__(cfg, session, cache, timeout)
        self._api_key = self.cfg_opt(cfg, 'api_key')

    @classmethod
    def is_configured(cls, cfg) -> bool:
        return bool(cls.cfg_opt(cfg, 'api_key'))

    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[str]:
        queries = [q for q in (orig_title, title) if q]
        for q in queries:
            try:
                r = self._http.get(
                    f'{TMDB_API}/search/multi',
                    params={'api_key': self._api_key, 'query': q,
                            'language': 'hu-HU'},
                    timeout=self._timeout)
                r.raise_for_status()
            except requests.RequestException as e:
                log.warning(f'TMDB search failed for {q!r}: {e}')
                continue
            match = self._best_match(
                r.json().get('results', []), queries, year)
            if not match:
                continue
            if match.get('media_type') == 'tv' and season and episode:
                still = self._episode_still(
                    match['id'], season, episode)
                if still:
                    return TMDB_IMG + still
            if match.get('poster_path'):
                return TMDB_IMG + match['poster_path']
        return None

    def _best_match(self, results, queries, year):
        norm_queries = {norm_title(q) for q in queries}
        for res in results:
            if res.get('media_type') not in ('movie', 'tv'):
                continue
            titles = {res.get('title'), res.get('original_title'),
                      res.get('name'), res.get('original_name')}
            if not {norm_title(t) for t in titles if t} & norm_queries:
                continue
            if year:
                date = res.get('release_date') or \
                    res.get('first_air_date') or ''
                try:
                    if abs(int(date[:4]) - int(year)) > 1:
                        continue
                except ValueError:
                    continue
            return res
        return None

    def _episode_still(self, tv_id, season, episode):
        try:
            r = self._http.get(
                f'{TMDB_API}/tv/{tv_id}/season/{season}/episode/{episode}',
                params={'api_key': self._api_key},
                timeout=self._timeout)
            r.raise_for_status()
            return r.json().get('still_path')
        except requests.RequestException as e:
            log.debug(f'TMDB episode lookup failed {tv_id} '
                      f's{season}e{episode}: {e}')
            return None
