#!/usr/bin/env python3
"""Tests for the TMDB provider: search matching, the two-level
(series + episode) cache and episode-level field overrides."""

from unittest.mock import MagicMock

import pytest
import requests

from py_epg.metadata_providers.tmdb import TmdbMetadata, TMDB_IMG
from conftest import cfg_el, json_response


def make_provider(cache=None):
    cfg = cfg_el('<metadata provider="tmdb" tmdb_api_key="key"/>')
    p = TmdbMetadata(cfg, session=MagicMock(), cache=cache)
    p._genre_maps = {'tv': {}, 'movie': {}}  # no genre fetch in tests
    return p


SERIES = {
    'media_type': 'tv', 'id': 51585, 'name': 'Bogyó és Babóca',
    'original_name': 'Berry and Dolly', 'first_air_date': '2010-05-01',
    'overview': 'series desc', 'origin_country': ['HU'],
    'genre_ids': [], 'vote_average': 7.5, 'poster_path': '/poster.jpg',
}


def wire_session(session, search_results=None, episode=None,
                 episode_error=None):
    """Routes session.get: search/multi vs /tv/<id>/season/s/episode/e."""
    def route(url, params=None, timeout=None):
        if '/episode/' in url:
            if episode_error is not None:
                r = MagicMock()
                r.raise_for_status.side_effect = episode_error
                return r
            return json_response(episode or {})
        return json_response({'results': search_results or []})
    session.get.side_effect = route


def search_calls(session):
    return [c for c in session.get.call_args_list
            if 'search/multi' in c.args[0]]


def episode_calls(session):
    return [c for c in session.get.call_args_list
            if '/episode/' in c.args[0]]


class TestSeriesLevelCaching:
    """The TMDB entry match is shared by all episodes of a series."""

    def test_episodes_share_one_search(self):
        p = make_provider()
        wire_session(p._http, [SERIES], episode={'name': 'Ep'})
        p.metadata_for('Bogyó és Babóca', season=4, episode=5)
        p.metadata_for('Bogyó és Babóca', season=4, episode=6)
        p.metadata_for('Bogyó és Babóca', season=5, episode=1)
        assert len(search_calls(p._http)) == 1
        assert len(episode_calls(p._http)) == 3

    def test_series_match_survives_new_provider_instance(self, cache):
        p1 = make_provider(cache=cache)
        wire_session(p1._http, [SERIES], episode={'name': 'Ep'})
        p1.metadata_for('Bogyó és Babóca', season=4, episode=5)
        # a fresh provider (e.g. next run / other worker process) must
        # not search again - only the per-episode call happens
        p2 = make_provider(cache=cache)
        p2._genre_maps = {'tv': {}, 'movie': {}}
        wire_session(p2._http, [], episode={'name': 'Ep2'})
        meta = p2.metadata_for('Bogyó és Babóca', season=4, episode=6)
        assert len(search_calls(p2._http)) == 0
        assert meta['episode_title'] == 'Ep2'
        # same episode again: the per-episode cache entry covers it
        p2._http.get.reset_mock()
        meta = p2.metadata_for('Bogyó és Babóca', season=4, episode=6)
        assert meta['episode_title'] == 'Ep2'
        assert p2._http.get.call_count == 0

    def test_negative_series_match_shared(self):
        p = make_provider()
        wire_session(p._http, [])
        assert p.metadata_for('Nope', season=1, episode=1) == {}
        assert p.metadata_for('Nope', season=1, episode=2) == {}
        assert len(search_calls(p._http)) == 1
        assert len(episode_calls(p._http)) == 0

    def test_failed_search_not_cached(self):
        p = make_provider()
        p._http.get.side_effect = requests.ConnectionError('boom')
        with pytest.raises(requests.ConnectionError):
            p.metadata_for('Bogyó és Babóca', season=1, episode=1)
        wire_session(p._http, [SERIES], episode={'name': 'Ep'})
        meta = p.metadata_for('Bogyó és Babóca', season=1, episode=1)
        assert meta['episode_title'] == 'Ep'


class TestBestMatch:
    def setup_method(self):
        self.p = make_provider()

    def test_exact_normalised_title(self):
        res = [{'media_type': 'tv', 'name': 'Bogyo es Baboca'}]
        m = self.p._best_match(res, ['Bogyó és Babóca'], None)
        assert m is res[0]

    def test_original_name_matches_orig_title(self):
        res = [{'media_type': 'tv', 'name': 'Vadicuccok',
                'original_name': 'Berry and Dolly'}]
        m = self.p._best_match(res, ['Berry and Dolly', 'Vadicuccok'],
                               None)
        assert m is res[0]

    def test_non_movie_tv_skipped(self):
        res = [{'media_type': 'person', 'name': 'X'},
               {'media_type': 'movie', 'title': 'X'}]
        assert self.p._best_match(res, ['X'], None) is res[1]

    def test_year_within_one(self):
        res = [{'media_type': 'movie', 'title': 'Film',
                'release_date': '2011-01-01'}]
        assert self.p._best_match(res, ['Film'], '2010') is res[0]
        assert self.p._best_match(res, ['Film'], '2013') is None

    def test_bad_date_rejected_when_year_known(self):
        res = [{'media_type': 'movie', 'title': 'Film',
                'release_date': ''}]
        assert self.p._best_match(res, ['Film'], '2010') is None
        # ...but accepted when no year is given
        assert self.p._best_match(res, ['Film'], None) is res[0]

    def test_no_title_overlap_no_match(self):
        res = [{'media_type': 'tv', 'name': 'Something Else'}]
        assert self.p._best_match(res, ['Show'], None) is None


class TestSearch:
    def test_orig_title_queried_first(self):
        p = make_provider()
        wire_session(p._http, [{'media_type': 'tv', 'name': 'Hit',
                                'id': 1}])
        p.metadata_for('Magyar Cím', orig_title='English Title')
        q = search_calls(p._http)[0].kwargs['params']['query']
        assert q == 'English Title'

    def test_falls_back_to_hungarian_title(self):
        p = make_provider()
        calls = []

        def route(url, params=None, timeout=None):
            calls.append(params['query'])
            # orig_title finds nothing, hu title matches
            results = [] if params['query'] == 'English' else \
                [{'media_type': 'tv', 'name': 'Magyar', 'id': 1}]
            return json_response({'results': results})
        p._http.get.side_effect = route
        p.metadata_for('Magyar', orig_title='English')
        assert calls == ['English', 'Magyar']

    def test_no_match_returns_none(self):
        p = make_provider()
        wire_session(p._http, [])
        assert p._lookup('X', None, None, None, None) is None


class TestToMetadata:
    def setup_method(self):
        self.p = make_provider()

    def test_series_fields(self):
        meta = self.p._to_metadata(dict(SERIES), None, None)
        assert meta['title'] == 'Bogyó és Babóca'
        assert meta['orig_title'] == 'Berry and Dolly'
        assert meta['desc'] == 'series desc'
        assert meta['year'] == '2010'
        assert meta['countries'] == ['HU']
        assert meta['icon'] == TMDB_IMG + '/poster.jpg'
        assert meta['rating'] == '7.5/10'
        assert meta['rating_system'] == 'tmdb'

    def test_episode_overrides(self):
        p = make_provider()
        ep = {'name': 'A Napló', 'overview': 'ep desc',
              'still_path': '/still.jpg'}
        wire_session(p._http, episode=ep)
        meta = p._to_metadata(dict(SERIES), 4, 5)
        assert meta['icon'] == TMDB_IMG + '/still.jpg'
        assert meta['desc'] == 'ep desc'
        assert meta['episode_title'] == 'A Napló'

    def test_episode_partial_override(self):
        # only a name - poster/desc stay series-level
        p = make_provider()
        wire_session(p._http, episode={'name': 'Ep'})
        meta = p._to_metadata(dict(SERIES), 4, 5)
        assert meta['icon'] == TMDB_IMG + '/poster.jpg'
        assert meta['desc'] == 'series desc'
        assert meta['episode_title'] == 'Ep'

    def test_episode_404_keeps_series_fields(self):
        p = make_provider()
        wire_session(p._http, episode_error=requests.HTTPError('404'))
        meta = p._to_metadata(dict(SERIES), 4, 6)
        assert meta['icon'] == TMDB_IMG + '/poster.jpg'
        assert 'episode_title' not in meta

    def test_movie_match_skips_episode_fetch(self):
        p = make_provider()
        wire_session(p._http)
        movie = {'media_type': 'movie', 'title': 'Film', 'id': 1}
        p._to_metadata(movie, 4, 5)
        assert episode_calls(p._http) == []

    def test_no_season_episode_skips_fetch(self):
        p = make_provider()
        wire_session(p._http)
        p._to_metadata(dict(SERIES), None, None)
        p._to_metadata(dict(SERIES), 4, None)
        assert episode_calls(p._http) == []

    def test_empty_values_dropped(self):
        meta = self.p._to_metadata({'media_type': 'movie', 'title': 'F'},
                                   None, None)
        assert meta == {'title': 'F'}


class TestGenres:
    def test_genre_names_mapped_and_cached(self, cache):
        p = make_provider(cache=cache)
        p._genre_maps = {}
        wire_session(p._http)
        p._http.get.side_effect = lambda *a, **k: json_response(
            {'genres': [{'id': 16, 'name': 'Animáció'},
                        {'id': 35, 'name': 'Vígjáték'}]})
        names = p._genre_names('tv', [16, 35, 999])
        assert names == ['Animáció', 'Vígjáték']
        # cached: a second instance maps without any request
        p2 = make_provider(cache=cache)
        p2._genre_maps = {}
        assert p2._genre_names('tv', [16]) == ['Animáció']
        assert p2._http.get.call_count == 0

    def test_genre_fetch_failure_returns_none(self):
        p = make_provider()
        p._genre_maps = {}
        p._http.get.side_effect = requests.ConnectionError('boom')
        assert p._genre_names('tv', [16]) is None
