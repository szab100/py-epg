#!/usr/bin/env python3
"""Optional programme-metadata providers (poster/artwork enrichment).

Adding a new provider:
    1. create a module under py_epg/metadata_providers/
    2. subclass MetadataProvider, set a unique 'name' (the <metadata
       provider="..."> token), and implement _lookup()
    No registration step needed - providers are auto-discovered.
"""

import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from typing import Optional

import requests

log = logging.getLogger(__name__)


def norm_title(title: str) -> str:
    """Accent/punctuation-insensitive title normalisation for matching."""
    t = unicodedata.normalize('NFKD', title or '')
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', t.lower())


class MetadataProvider(ABC):
    """
    Looks up programme artwork. Returns an absolute image URL or None.

    'cfg' is the <metadata> config element and is only guaranteed to exist
    during __init__ - extract what you need, do not store the lxml element
    (providers are pickled into worker processes).
    """

    name = ''

    def __init__(self, cfg, session: requests.Session,
                 cache=None, timeout: int = 15):
        self._http = session
        self._cache = cache
        self._timeout = timeout
        # per-process memo - skips the SQLite round-trip for repeated
        # lookups within a run (same title airing N times)
        self._memo = {}

    @classmethod
    def cfg_opt(cls, cfg, key: str, default: str = '') -> str:
        """
        Reads a provider-specific <metadata> attribute, e.g. for name
        'tmdb' and key 'api_key' looks up 'tmdb_api_key'. Falls back to
        the legacy shared 'api-key' attribute for that key.
        """
        value = cfg.attrib.get(f'{cls.name}_{key}', default).strip()
        if not value and key == 'api_key':
            value = cfg.attrib.get('api-key', '').strip()
        return value

    @classmethod
    def is_configured(cls, cfg) -> bool:
        """Whether <metadata> carries everything this provider needs."""
        return True

    def metadata_for(self, title: str, orig_title: Optional[str] = None,
                     year: Optional[str] = None,
                     season: Optional[int] = None,
                     episode: Optional[int] = None) -> dict:
        """
        Returns a dict of resolved fields ('icon', 'desc', 'title',
        'orig_title', 'year', 'genres', 'countries', 'rating',
        'rating_system', 'episode_title') or an empty dict on no match.
        """
        if year:
            # sources send things like '2021.' - providers get a clean year
            m = re.search(r'\d{4}', str(year))
            year = m.group(0) if m else None
        cache_key = self._cache_key(title, orig_title, year,
                                    season, episode)
        if cache_key in self._memo:
            log.debug(f'{self.name}: memo hit for {title!r}')
            return self._memo[cache_key]
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                # Legacy entries like {'icon': null} (written by older
                # versions, incl. failed lookups that were wrongly
                # cached) normalize to {} and are refetched so the
                # bogus value gets overwritten with a real result.
                meta = {k: v for k, v in hit.items() if v}
                if hit and not meta:
                    pass  # poisoned - fall through to _lookup
                else:
                    if meta:
                        meta.setdefault('_src', self.name)
                    log.debug(f'{self.name}: cache hit for {title!r}: '
                              f'{[k for k in meta if k != "_src"] or "no match"}')
                    self._memo[cache_key] = meta
                    return meta
        meta = self._lookup(title, orig_title, year, season, episode) or {}
        if meta:
            meta['_src'] = self.name
        log.debug(f'{self.name}: lookup {title!r} -> '
                  f'{[k for k in meta if k != "_src"] or "no match"}')
        if self._cache is not None:
            # negative results are cached too (empty dict)
            self._cache.set(cache_key, meta, 'meta')
        self._memo[cache_key] = meta
        return meta

    def _cache_key(self, title, orig_title, year, season, episode):
        return f'meta:{self.name}:{norm_title(title)}:' \
               f'{norm_title(orig_title or "")}:{year or ""}:' \
               f'{season or ""}:{episode or ""}'

    @abstractmethod
    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[dict]:
        """Provider-specific lookup. Returns a field dict or None."""


class ChainedMetadata(MetadataProvider):
    """
    Queries providers in order and returns the first non-empty result -
    providers listed earlier in provider="a,b" take priority, and later
    ones are skipped entirely on a match (port.hu stays a fallback for
    titles faster providers can't resolve).
    """

    def __init__(self, providers):
        self._providers = providers

    def metadata_for(self, title, orig_title=None, year=None,
                     season=None, episode=None):
        for p in self._providers:
            try:
                meta = p.metadata_for(
                    title, orig_title=orig_title, year=year,
                    season=season, episode=episode)
            except Exception as e:
                log.warning(f'{type(p).__name__} lookup failed for '
                            f'{title!r}: {e}')
                continue
            if meta:
                log.debug(f'chain: {p.name} matched {title!r}')
                return meta
        log.debug(f'chain: no provider matched {title!r}')
        return {}

    def _lookup(self, title, orig_title, year, season, episode):
        raise NotImplementedError


def build_metadata(cfg, session, cache=None, timeout: int = 15):
    """Creates a MetadataProvider from the <metadata> config element."""
    if cfg is None:
        return None
    available = {cls.name: cls for cls in MetadataProvider.__subclasses__()
                 if cls.name}
    providers = []
    for provider in cfg.attrib.get('provider', '').lower().split(','):
        provider = provider.strip()
        if not provider:
            continue
        cls = available.get(provider)
        if cls is None:
            log.warning(f'Unknown metadata provider: {provider!r}')
        elif not cls.is_configured(cfg):
            log.warning(f'Metadata provider {provider!r} is missing '
                        f'required configuration')
        else:
            providers.append(cls(cfg, session=session, cache=cache,
                                 timeout=timeout))
    if not providers:
        return None
    return providers[0] if len(providers) == 1 \
        else ChainedMetadata(providers)
