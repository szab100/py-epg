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

    def icon_for(self, title: str, orig_title: Optional[str] = None,
                 year: Optional[str] = None,
                 season: Optional[int] = None,
                 episode: Optional[int] = None) -> Optional[str]:
        cache_key = self._cache_key(title, orig_title, year,
                                    season, episode)
        if self._cache is not None:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit['icon']
        icon = self._lookup(title, orig_title, year, season, episode)
        if self._cache is not None:
            # negative results are cached too - 'icon': None
            self._cache.set(cache_key, {'icon': icon}, 'meta')
        return icon

    def _cache_key(self, title, orig_title, year, season, episode):
        return f'meta:{self.name}:{norm_title(title)}:' \
               f'{norm_title(orig_title or "")}:{year or ""}:' \
               f'{season or ""}:{episode or ""}'

    @abstractmethod
    def _lookup(self, title, orig_title, year,
                season, episode) -> Optional[str]:
        """Provider-specific lookup. Returns an absolute URL or None."""


class ChainedMetadata(MetadataProvider):
    """Tries each provider in order; returns the first icon found."""

    def __init__(self, providers):
        self._providers = providers

    def icon_for(self, title, orig_title=None, year=None,
                 season=None, episode=None):
        for p in self._providers:
            icon = p.icon_for(title, orig_title=orig_title, year=year,
                              season=season, episode=episode)
            if icon:
                return icon
        return None

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
