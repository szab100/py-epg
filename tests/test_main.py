#!/usr/bin/env python3
"""Tests for main.py: the batched metadata pass on the worker pool,
per-programme field application and stop-time synthesis."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from xmltv.models import (Category, Country, Desc, Icon, Programme,
                          Rating, SubTitle, Title)

from py_epg.main import PyEPG


def make_app(metadata=None, pool_size=1):
    """A PyEPG instance without __init__ (which needs argv/config)."""
    app = PyEPG.__new__(PyEPG)
    app._log = logging.getLogger('test')
    app._args = SimpleNamespace(progress_bar=False, quiet=True)
    app._metadata = metadata
    app._pool_size = pool_size

    class FakePool:
        """Runs tasks synchronously, like imap_unordered does."""
        def __init__(self):
            self.func = None
            self.args = None

        def imap_unordered(self, fn, iterable, chunksize=1):
            self.func = fn
            self.args = list(iterable)
            return (fn(a) for a in self.args)

    app._pool = FakePool()
    return app


def prog(title='Cím', start='20240115060000 +0100', meta_args=None):
    p = Programme(channel='CH', start=start,
                  title=[Title(content=[title], lang='hu')])
    if meta_args is not None:
        p._meta_lookup = meta_args
    return p


class TestResolveMetadata:
    def test_success_returns_meta(self):
        m = MagicMock()
        m.metadata_for.return_value = {'icon': 'x'}
        app = make_app(metadata=m)
        args, meta, err = app._resolve_metadata(('T', None, None, 1, 1))
        assert (args, meta, err) == (('T', None, None, 1, 1),
                                     {'icon': 'x'}, None)
        m.metadata_for.assert_called_once_with('T', None, None, 1, 1)

    def test_error_returned_not_raised(self):
        m = MagicMock()
        m.metadata_for.side_effect = RuntimeError('boom')
        app = make_app(metadata=m)
        args, meta, err = app._resolve_metadata(('T',))
        assert meta is None and err == 'RuntimeError: boom'


class TestApplyMetadata:
    def test_identical_lookups_deduplicated(self):
        m = MagicMock()
        m.metadata_for.return_value = {'icon': 'http://x/i.jpg',
                                       '_src': 'tmdb'}
        app = make_app(metadata=m)
        args = ('Show', None, '2010', 4, 5)
        programs = [prog(meta_args=args) for _ in range(3)]
        app._apply_metadata(programs)
        m.metadata_for.assert_called_once_with(*args)
        assert all(p.icon[0].src == 'http://x/i.jpg' for p in programs)

    def test_distinct_episodes_resolved_separately(self):
        m = MagicMock()
        m.metadata_for.return_value = {}
        app = make_app(metadata=m)
        app._apply_metadata([
            prog(meta_args=('Show', None, '2010', 4, 5)),
            prog(meta_args=('Show', None, '2010', 4, 6)),
        ])
        assert m.metadata_for.call_count == 2

    def test_failed_lookup_leaves_programme_untouched(self):
        m = MagicMock()
        m.metadata_for.side_effect = RuntimeError('boom')
        app = make_app(metadata=m)
        p = prog(meta_args=('Show', None, None, None, None))
        app._apply_metadata([p])   # must not raise
        assert not p.icon

    def test_programmes_without_tag_skipped(self):
        m = MagicMock()
        app = make_app(metadata=m)
        app._apply_metadata([prog()])
        m.metadata_for.assert_not_called()

    def test_noop_without_provider(self):
        app = make_app(metadata=None)
        app._apply_metadata([prog(meta_args=('T',))])  # early return


class TestApplyProgramMetadata:
    def apply(self, p, meta):
        return PyEPG._apply_program_metadata(p, meta)

    def test_icon_always_wins(self):
        p = prog()
        p.icon = [Icon(src='http://site/old.jpg')]
        self.apply(p, {'icon': 'http://meta/new.jpg'})
        assert p.icon[0].src == 'http://meta/new.jpg'

    def test_orig_title_appended_once(self):
        p = prog()
        self.apply(p, {'orig_title': 'Original'})
        self.apply(p, {'orig_title': 'Original'})
        assert len(p.title) == 2
        assert p.title[1].lang == 'en'

    def test_episode_title_fills_empty_sub_title_only(self):
        p = prog()
        self.apply(p, {'episode_title': 'Ep A'})
        assert p.sub_title[0].content[0] == 'Ep A'
        # existing sub_title (e.g. from the source) is kept
        p2 = prog()
        p2.sub_title = [SubTitle(content=['Saját alcím'], lang='hu')]
        self.apply(p2, {'episode_title': 'Ep A'})
        assert p2.sub_title[0].content[0] == 'Saját alcím'

    def test_desc_year_countries_genres_fill_gaps(self):
        p = prog()
        p.desc = [Desc(content=['saját'])]
        p.date = '2001'
        p.country = [Country(content=['magyar'])]
        p.category = [Category(content=['sorozat'])]
        self.apply(p, {'desc': 'meta', 'year': '2010',
                       'countries': ['HU'], 'genres': ['anim']})
        assert p.desc[0].content[0] == 'saját'
        assert p.date == '2001'
        assert p.country[0].content[0] == 'magyar'
        assert p.category[0].content[0] == 'sorozat'

    def test_gaps_filled_when_empty(self):
        p = prog()
        self.apply(p, {'desc': 'meta', 'year': '2010',
                       'countries': ['HU'], 'genres': ['anim']})
        assert p.desc[0].content[0] == 'meta'
        assert p.date == '2010'
        assert p.country[0].content[0] == 'HU'
        assert p.category[0].content[0] == 'anim'

    def test_rating_appended(self):
        p = prog()
        self.apply(p, {'rating': '7.5/10', 'rating_system': 'tmdb'})
        assert p.rating[0].value == '7.5/10'
        assert p.rating[0].system == 'tmdb'

    def test_empty_meta_returns_zero(self):
        assert self.apply(prog(), {}) == 0


class TestPostProcessPrograms:
    def test_real_stop_kept(self):
        p = prog(start='20240115060000 +0100')
        p.stop = '20240115063000 +0100'
        app = make_app()
        app._post_process_programs([p])
        assert p.stop == '20240115063000 +0100'

    def test_stop_from_next_start_same_channel(self):
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115063000 +0100')
        make_app()._post_process_programs([p1, p2])
        assert p1.stop == '20240115063000 +0100'

    def test_last_programme_gets_end_of_day(self):
        p = prog(start='20240115060000 +0100')
        make_app()._post_process_programs([p])
        assert p.stop == '20240115235959 +0100'

    def test_next_programme_other_channel_gets_end_of_day(self):
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115063000 +0100')
        p2.channel = 'OTHER'
        make_app()._post_process_programs([p1, p2])
        assert p1.stop == '20240115235959 +0100'


class TestDedupePrograms:
    def test_exact_duplicates_removed(self):
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115060000 +0100')
        p3 = prog(start='20240115060000 +0100')
        result = make_app()._dedupe_programs([p1, p2, p3])
        assert result == [p1]

    def test_different_starts_kept(self):
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115063000 +0100')
        result = make_app()._dedupe_programs([p1, p2])
        assert result == [p1, p2]

    def test_different_channels_kept(self):
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115060000 +0100')
        p2.channel = 'OTHER'
        result = make_app()._dedupe_programs([p1, p2])
        assert result == [p1, p2]

    def test_different_stops_kept(self):
        p1 = prog(start='20240115060000 +0100')
        p1.stop = '20240115070000 +0100'
        p2 = prog(start='20240115060000 +0100')
        p2.stop = '20240115073000 +0100'
        result = make_app()._dedupe_programs([p1, p2])
        assert result == [p1, p2]

    def test_dedup_before_stop_synthesis_fixes_zero_length(self):
        """Triplicated listings produced start==stop programmes."""
        p1 = prog(start='20240115060000 +0100')
        p2 = prog(start='20240115060000 +0100')
        p3 = prog(start='20240115070000 +0100')
        app = make_app()
        programs = app._dedupe_programs([p1, p2, p3])
        app._post_process_programs(programs)
        assert programs[0].stop == '20240115070000 +0100'
