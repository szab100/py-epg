#!/usr/bin/env python3
"""Optional programme-metadata providers (poster/artwork enrichment)."""

import logging
import re
import unicodedata
from typing import Optional

import requests

log = logging.getLogger(__name__)

TMDB_API = 'https://api.themoviedb.org/3'
TMDB_IMG = 'https://image.tmdb.org/t/p/w500'


def _norm(title: str) -> str:
    t = unicodedata.normalize('NFKD', title or '')
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', t.lower())


class MetadataProvider:
    """Looks up programme artwork. Returns an absolute image URL or None."""

    def icon_for(self, title: str, orig_title: Optional[str] = None,
                 year: Optional[str] = None,
                 season: Optional[int] = None,
                 episode: Optional[int] = None) -> Optional[str]:
        raise NotImplementedError


class TmdbMetadata(MetadataProvider):
    """
    TMDB-backed provider.

    Matching strategy (a 'proper match' is required - no fuzzy guesses):
      1. search/multi with the original (English) title when present,
         else the Hungarian title with language=hu-HU
      2. accept a hit only when the normalised title/original_title equals
         the query title, and the year (when known) is within +-1
      3. for TV hits with season+episode, prefer the episode still over
         the series poster
    """

    def __init__(self, api_key: str, session: requests.Session,
                 cache=None, timeout: int = 15):
        self._api_key = api_key
        self._http = session
        self._cache = cache
        self._timeout = timeout

    def icon_for(self, title, orig_title=None, year=None,
                 season=None, episode=None):
        cache_key = f'meta:tmdb:{_norm(title)}:{_norm(orig_title or "")}:' \
                    f'{year or ""}:{season or ""}:{episode or ""}'
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit['icon']
        icon = self._lookup(title, orig_title, year, season, episode)
        if self._cache is not None:
            # negative results are cached too - 'icon': None
            self._cache.set(cache_key, {'icon': icon}, 'meta')
        return icon

    def _lookup(self, title, orig_title, year, season, episode):
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
        norm_queries = {_norm(q) for q in queries}
        for res in results:
            if res.get('media_type') not in ('movie', 'tv'):
                continue
            titles = {res.get('title'), res.get('original_title'),
                      res.get('name'), res.get('original_name')}
            if not {_norm(t) for t in titles if t} & norm_queries:
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


def build_metadata(cfg, session, cache=None, timeout: int = 15):
    """Creates a MetadataProvider from the <metadata> config element."""
    if cfg is None:
        return None
    provider = cfg.attrib.get('provider', '').lower()
    api_key = cfg.attrib.get('api-key', '').strip()
    if provider == 'tmdb' and api_key:
        return TmdbMetadata(api_key, session=session, cache=cache,
                            timeout=timeout)
    if provider:
        log.warning(f'Unknown/incomplete metadata provider config: '
                    f'{provider!r}')
    return None
