#!/usr/bin/env python3
"""TMDB-backed programme metadata provider."""

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
      3. for TV hits with season+episode, prefer the episode still,
         episode title and episode synopsis over the series-level data

    Requires a free tmdb_api_key attribute on <metadata> (themoviedb.org
    -> Settings -> API).
    """

    name = 'tmdb'

    def __init__(self, cfg, session: requests.Session,
                 cache=None, timeout: int = 15):
        super().__init__(cfg, session, cache, timeout)
        self._api_key = self.cfg_opt(cfg, 'api_key')
        self._genre_maps = {}

    @classmethod
    def is_configured(cls, cfg) -> bool:
        return bool(cls.cfg_opt(cfg, 'api_key'))

    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[dict]:
        queries = [q for q in (orig_title, title) if q]
        for q in queries:
            # RequestExceptions propagate: a transient failure must not
            # be stored as a negative cache entry.
            r = self._http.get(
                f'{TMDB_API}/search/multi',
                params={'api_key': self._api_key, 'query': q,
                        'language': 'hu-HU'},
                timeout=self._timeout)
            r.raise_for_status()
            match = self._best_match(
                r.json().get('results', []), queries, year)
            if match:
                return self._to_metadata(match, season, episode)
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

    def _to_metadata(self, match, season, episode) -> dict:
        meta = {
            'title': match.get('title') or match.get('name'),
            'orig_title': match.get('original_title')
            or match.get('original_name'),
            'desc': match.get('overview') or None,
            'year': (match.get('release_date')
                     or match.get('first_air_date') or '')[:4] or None,
            'countries': match.get('origin_country') or None,
            'genres': self._genre_names(
                match.get('media_type'), match.get('genre_ids')),
        }
        if match.get('poster_path'):
            meta['icon'] = TMDB_IMG + match['poster_path']
        if match.get('vote_average'):
            meta['rating'] = f"{match['vote_average']:.1f}/10"
            meta['rating_system'] = 'tmdb'
        ep = self._episode(match, season, episode)
        if ep:
            if ep.get('still_path'):
                meta['icon'] = TMDB_IMG + ep['still_path']
            if ep.get('overview'):
                meta['desc'] = ep['overview']
            if ep.get('name'):
                meta['episode_title'] = ep['name']
        return {k: v for k, v in meta.items() if v}

    def _episode(self, match, season, episode) -> Optional[dict]:
        if match.get('media_type') != 'tv' or not (season and episode):
            return None
        try:
            r = self._http.get(
                f'{TMDB_API}/tv/{match["id"]}/season/{season}'
                f'/episode/{episode}',
                params={'api_key': self._api_key, 'language': 'hu-HU'},
                timeout=self._timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.debug(f'TMDB episode lookup failed {match["id"]} '
                      f's{season}e{episode}: {e}')
            return None

    def _genre_names(self, media_type, genre_ids):
        """Maps TMDB genre ids to localised names (hu-HU)."""
        if not genre_ids or media_type not in ('movie', 'tv'):
            return None
        gmap = self._genres_for(media_type)
        names = [gmap.get(i) for i in genre_ids]
        return [n for n in names if n] or None

    def _genres_for(self, media_type) -> dict:
        if media_type in self._genre_maps:
            return self._genre_maps[media_type]
        gmap = {}
        cache_key = f'meta:tmdb:genres:{media_type}'
        if self._cache is not None:
            gmap = self._cache.get(cache_key) or {}
        if not gmap:
            try:
                r = self._http.get(
                    f'{TMDB_API}/genre/{media_type}/list',
                    params={'api_key': self._api_key,
                            'language': 'hu-HU'},
                    timeout=self._timeout)
                r.raise_for_status()
                gmap = {g['id']: g['name']
                        for g in r.json().get('genres', [])}
            except requests.RequestException as e:
                log.warning(f'TMDB genre list fetch failed: {e}')
            if gmap and self._cache is not None:
                self._cache.set(cache_key, gmap, 'meta')
        self._genre_maps[media_type] = gmap
        return gmap
