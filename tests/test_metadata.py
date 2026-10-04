#!/usr/bin/env python3
"""Tests for the provider framework: caching/memoisation in
MetadataProvider.metadata_for, the ChainedMetadata fallback order and
build_metadata's config-driven wiring."""

import pickle
from unittest.mock import MagicMock

import pytest
import requests

from py_epg.common.metadata import (ChainedMetadata, MetadataProvider,
                                    build_metadata, norm_title)
# importing the provider modules registers them for build_metadata
import py_epg.metadata_providers.tmdb  # noqa: F401
import py_epg.metadata_providers.porthu  # noqa: F401
from conftest import cfg_el


class DummyProvider(MetadataProvider):
    name = 'dummy'

    def _lookup(self, title, orig_title, year, season, episode):
        return self.lookup_fn(title, orig_title, year, season, episode)


def make_provider(cache=None, return_value=None):
    p = DummyProvider(cfg=None, session=MagicMock(), cache=cache)
    p.lookup_fn = MagicMock(return_value=return_value)
    return p


class TestNormTitle:
    def test_strips_accents_punct_case(self):
        assert norm_title('Bogyó és Babóca!') == 'bogyoesbaboca'
        assert norm_title('  Kép: Múlt-Idő (2020) ') == 'kepmultido2020'

    def test_none_and_empty(self):
        assert norm_title(None) == ''
        assert norm_title('') == ''


class TestMetadataFor:
    def test_memo_hit_skips_lookup(self):
        p = make_provider(return_value={'icon': 'x'})
        args = ('Show', 'Original', '2010', 1, 2)
        assert p.metadata_for(*args)['icon'] == 'x'
        assert p.metadata_for(*args)['icon'] == 'x'
        assert p.lookup_fn.call_count == 1

    def test_distinct_episodes_are_distinct_lookups(self):
        p = make_provider(return_value={'icon': 'x'})
        p.metadata_for('Show', season=1, episode=1)
        p.metadata_for('Show', season=1, episode=2)
        assert p.lookup_fn.call_count == 2

    def test_result_cached_persistently(self, cache):
        p1 = make_provider(cache=cache, return_value={'icon': 'x'})
        p1.metadata_for('Show', season=1, episode=1)
        # fresh provider instance (new memo) sees the cached result
        p2 = make_provider(cache=cache)
        meta = p2.metadata_for('Show', season=1, episode=1)
        assert meta['icon'] == 'x'
        p2.lookup_fn.assert_not_called()

    def test_negative_result_cached(self, cache):
        p1 = make_provider(cache=cache, return_value=None)
        assert p1.metadata_for('Nothing') == {}
        p2 = make_provider(cache=cache)
        assert p2.metadata_for('Nothing') == {}
        p2.lookup_fn.assert_not_called()

    def test_poisoned_legacy_entry_refetched(self, cache):
        # older versions wrote falsy-only dicts like {'icon': None}
        key = make_provider()._cache_key('Show', None, None, None, None)
        cache.set(key, {'icon': None}, 'meta')
        p = make_provider(cache=cache, return_value={'icon': 'real'})
        assert p.metadata_for('Show')['icon'] == 'real'
        p.lookup_fn.assert_called_once()
        # and the poisoned row was overwritten with the real result
        assert cache.get(key)['icon'] == 'real'

    def test_year_normalised(self):
        p = make_provider(return_value={})
        p.metadata_for('Show', year='2021.')
        assert p.lookup_fn.call_args[0][2] == '2021'

    def test_lookup_exception_propagates_and_is_not_cached(self, cache):
        p = make_provider(cache=cache)
        p.lookup_fn.side_effect = requests.ConnectionError('boom')
        with pytest.raises(requests.ConnectionError):
            p.metadata_for('Show')
        # nothing cached -> retried next run
        p.lookup_fn.side_effect = None
        p.lookup_fn.return_value = {'icon': 'x'}
        assert p.metadata_for('Show')['icon'] == 'x'

    def test_src_tagged_on_hit(self, cache):
        p = make_provider(cache=cache, return_value={'icon': 'x'})
        assert p.metadata_for('Show')['_src'] == 'dummy'


class TestChainedMetadata:
    def test_first_non_empty_wins(self):
        a = make_provider(return_value={'icon': 'a'})
        b = make_provider(return_value={'icon': 'b'})
        chain = ChainedMetadata([a, b])
        assert chain.metadata_for('Show')['icon'] == 'a'
        b.lookup_fn.assert_not_called()

    def test_fallback_on_empty(self):
        a = make_provider(return_value=None)
        b = make_provider(return_value={'icon': 'b'})
        chain = ChainedMetadata([a, b])
        assert chain.metadata_for('Show')['icon'] == 'b'

    def test_provider_exception_falls_through(self):
        a = make_provider()
        a.lookup_fn.side_effect = RuntimeError('broken')
        b = make_provider(return_value={'icon': 'b'})
        chain = ChainedMetadata([a, b])
        assert chain.metadata_for('Show')['icon'] == 'b'

    def test_all_empty(self):
        chain = ChainedMetadata(
            [make_provider(), make_provider()])
        assert chain.metadata_for('Show') == {}


class TestBuildMetadata:
    def test_none_cfg(self):
        assert build_metadata(None, session=MagicMock()) is None

    def test_unknown_provider_skipped(self):
        cfg = cfg_el('<metadata provider="bogus"/>')
        assert build_metadata(cfg, session=MagicMock()) is None

    def test_unconfigured_provider_skipped(self):
        # tmdb without an api key is not configured
        cfg = cfg_el('<metadata provider="tmdb"/>')
        assert build_metadata(cfg, session=MagicMock()) is None

    def test_single_provider_returned_directly(self):
        cfg = cfg_el('<metadata provider="porthu"/>')
        m = build_metadata(cfg, session=MagicMock())
        assert type(m).__name__ == 'PortHuMetadata'

    def test_multiple_providers_chained(self):
        cfg = cfg_el(
            '<metadata provider="tmdb,porthu" tmdb_api_key="x"/>')
        m = build_metadata(cfg, session=MagicMock())
        assert type(m).__name__ == 'ChainedMetadata'

    def test_providers_pickle_for_worker_pool(self):
        cfg = cfg_el(
            '<metadata provider="tmdb,porthu" tmdb_api_key="x"/>')
        m = build_metadata(cfg, session=requests.Session())
        clone = pickle.loads(pickle.dumps(m))
        assert type(clone).__name__ == 'ChainedMetadata'
        assert [type(p).__name__ for p in clone._providers] == [
            'TmdbMetadata', 'PortHuMetadata']


