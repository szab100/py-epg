#!/usr/bin/env python3
"""Tests for the port.hu scraper: season/episode parsing, JSON-LD
detail extraction and programme assembly."""

import json
import pickle
from datetime import date
from unittest.mock import MagicMock

import pytest
import requests
from xmltv.models import Programme, Title

from py_epg.scrapers.port_hu import PortHu


def make_scraper(cache=None, metadata=None):
    s = PortHu(user_agent='test', cache=cache, metadata=metadata,
               request_delay=0)
    s._http = MagicMock()
    return s


def make_programme():
    return Programme(channel='CH', start='20240115060000 +0100',
                     title=[Title(content=['Cím'], lang='hu')])


class TestThrottlePickle:
    def test_pickled_task_copy_starts_cold(self):
        """A task copy can't inherit live throttle state - its first
        request must wait request_delay so task waves can't burst."""
        s = PortHu(user_agent='test', request_delay=5.0)
        assert s._last_request == 0.0
        clone = pickle.loads(pickle.dumps(s))
        assert clone._last_request > 0.0

    def test_zero_delay_copy_stays_hot(self):
        s = PortHu(user_agent='test', request_delay=0)
        clone = pickle.loads(pickle.dumps(s))
        assert clone._last_request == 0.0


class TestSeasonEpisode:
    @pytest.mark.parametrize('text,expected', [
        ('magyar animációs sorozat III/12. rész', (3, 12)),
        ('amerikai sorozat XIV/2. rész', (14, 2)),
        ('valami 5. rész', (None, 5)),
        ('nincs epizód', (None, None)),
        (None, (None, None)),
    ])
    def test_parse(self, text, expected):
        s = make_scraper()
        assert s._season_episode(text) == expected


class TestRomanToInt:
    @pytest.mark.parametrize('s,expected', [
        ('I', 1), ('IV', 4), ('IX', 9), ('XIV', 14), ('XL', 40),
        ('MCM', 1900), ('ABC', None), ('', None),
    ])
    def test_convert(self, s, expected):
        assert make_scraper()._roman_to_int(s) == expected


class TestXmltvTime:
    def test_iso_to_xmltv(self):
        s = make_scraper()
        assert s._xmltv_time('2024-01-15T14:30:00+01:00') == \
            '20240115143000 +0100'

    def test_empty(self):
        assert make_scraper()._xmltv_time(None) is None
        assert make_scraper()._xmltv_time('') is None


class TestParseDetailPage:
    def _page(self, **fields):
        ld = {'description': 'A leírás.', 'genre': 'animáció',
              'copyrightYear': 2010,
              'countryOfOrigin': [{'name': 'Magyarország'}],
              'director': [{'name': 'Dir Ector'}],
              'actor': [{'name': 'Act Or'}, {'name': 'Some One'}],
              'duration': 'PT1H25M',
              'aggregateRating': {'ratingValue': 7.4},
              'contentRating': 6,
              'image': {'url': 'http://x/poster.jpg'}}
        ld.update(fields)
        return (f'<html><script type="application/ld+json">'
                f'{json.dumps(ld)}</script></html>')

    def test_full_block(self):
        d = make_scraper()._parse_detail_page(self._page())
        assert d['desc'] == 'A leírás.'
        assert d['genre'] == 'animáció'
        assert d['year'] == 2010
        assert d['countries'] == ['Magyarország']
        assert d['directors'] == ['Dir Ector']
        assert d['actors'] == ['Act Or', 'Some One']
        assert d['length_min'] == '85'
        assert d['rating'] == '7.4/10'
        assert d['rating_system'] == 'port.hu-score'
        assert d['age_rating'] == '6'
        assert d['icon'] == 'http://x/poster.jpg'

    def test_no_ldjson_returns_empty(self):
        assert make_scraper()._parse_detail_page('<html></html>') == {}

    def test_ldjson_without_description_skipped(self):
        html = ('<script type="application/ld+json">'
                '{"name": "no desc"}</script>')
        assert make_scraper()._parse_detail_page(html) == {}

    def test_invalid_json_skipped(self):
        html = ('<script type="application/ld+json">{oops</script>'
                '<script type="application/ld+json">'
                '{"description": "ok"}</script>')
        d = make_scraper()._parse_detail_page(html)
        assert d['desc'] == 'ok'

    def test_minimal_block(self):
        d = make_scraper()._parse_detail_page(
            self._page(director=None, actor=None, duration=None,
                       aggregateRating=None, image=None,
                       countryOfOrigin=None, genre=None,
                       contentRating=None, copyrightYear=None))
        assert d['desc'] == 'A leírás.'
        assert d['rating'] is None
        assert d['icon'] is None


class TestApplyProgramDetails:
    def test_meta_lookup_tagged_with_episode(self):
        s = make_scraper(metadata=MagicMock())
        p = make_programme()
        e = {'title': 'Sorozat', 'short_description': 'sorozat II/5. rész',
             'is_repeat': False}
        s._apply_program_details(p, e, {'year': '2010'})
        assert p._meta_lookup == ('Sorozat', None, '2010', 2, 5)

    def test_no_metadata_no_tag(self):
        s = make_scraper(metadata=None)
        p = make_programme()
        s._apply_program_details(p, {'title': 'X'}, {})
        assert getattr(p, '_meta_lookup', None) is None

    def test_episode_num_both_formats(self):
        s = make_scraper()
        p = make_programme()
        s._apply_program_details(
            p, {'title': 'X', 'short_description': 'sorozat II/5. rész'},
            {})
        nums = {n.system: n.content[0] for n in p.episode_num}
        assert nums['onscreen'] == 'S02E05'
        assert nums['xmltv_ns'] == '1.4.'  # zero-based

    def test_episode_only_no_season(self):
        s = make_scraper()
        p = make_programme()
        s._apply_program_details(
            p, {'title': 'X', 'short_description': '7. rész'}, {})
        nums = {n.system: n.content[0] for n in p.episode_num}
        assert nums['onscreen'] == 'E07'
        assert nums['xmltv_ns'] == '6.'

    def test_details_applied(self):
        s = make_scraper()
        p = make_programme()
        e = {'title': 'X', 'episode_title': 'Ep cím',
             'is_repeat': True, 'age_limit': 12}
        details = {'desc': 'd', 'year': '1999', 'genre': 'dráma',
                   'countries': ['magyar'], 'directors': ['D'],
                   'actors': ['A'], 'length_min': '90',
                   'rating': '8.1/10', 'rating_system': 'port.hu-score',
                   'icon': 'http://x/i.jpg'}
        s._apply_program_details(p, e, details)
        assert p.icon[0].src == 'http://x/i.jpg'
        assert p.sub_title[0].content[0] == 'Ep cím'
        assert p.desc[0].content[0] == 'd'
        assert p.date == '1999'
        assert p.category[0].content[0] == 'dráma'
        assert p.country[0].content[0] == 'magyar'
        assert p.credits.director == ['D']
        assert p.credits.actor[0].content[0] == 'A'
        assert p.length.content[0] == '90'
        ratings = {r.system: r.value for r in p.rating}
        assert ratings['port.hu'] == '12'
        assert ratings['port.hu-score'] == '8.1/10'
        assert p.previously_shown is not None


class TestFetchPrograms:
    def test_listing_cached_and_programs_built(self, cache):
        s = make_scraper(cache=cache)
        events = {'channels': [{'programs': [
            {'id': 'e1', 'start_datetime': '2024-01-15T06:00:00+01:00',
             'end_datetime': '2024-01-15T06:30:00+01:00',
             'title': 'Reggeli', 'film_url': '/film/movie-1',
             'film_id': 'movie-1'},
            {'id': 'e2', 'start_datetime': '2024-01-15T06:30:00+01:00',
             'title': 'Második'},  # no film_url -> no detail fetch
        ]}]}
        s._get_json = MagicMock(return_value=events)
        s._get_program_details = MagicMock(return_value={'desc': 'd'})
        channel = MagicMock(id='CH')
        progs = s.fetch_programs(channel, 'tvchannel-1',
                                 date(2024, 1, 15))
        assert len(progs) == 2
        assert progs[0].start == '20240115060000 +0100'
        assert progs[0].stop == '20240115063000 +0100'
        assert progs[1].stop is None
        s._get_program_details.assert_called_once_with(
            'movie-1', '/film/movie-1')
        # listing cache: second call must not hit the API again
        s.fetch_programs(channel, 'tvchannel-1', date(2024, 1, 15))
        assert s._get_json.call_count == 1

    def test_episodes_of_same_series_get_distinct_detail_keys(self):
        """film_id is the series-level page id; episodes must key their
        details on the episode-level id from film_url - otherwise all
        episodes share one episode's details."""
        s = make_scraper()
        s._get_json = MagicMock(return_value={'channels': [{'programs': [
            {'id': 'e1', 'start_datetime': '2024-01-15T06:00:00+01:00',
             'title': 'Show', 'film_id': 'movie-1',
             'film_url': '/adatlap/x/event-tv-1/episode-100'},
            {'id': 'e2', 'start_datetime': '2024-01-15T07:00:00+01:00',
             'title': 'Show', 'film_id': 'movie-1',
             'film_url': '/adatlap/x/event-tv-2/episode-200'},
        ]}]})
        s._get_program_details = MagicMock(return_value={})
        s.fetch_programs(MagicMock(id='CH'), 'c', date(2024, 1, 15))
        calls = {c.args[0] for c in
                 s._get_program_details.call_args_list}
        assert calls == {'episode-100', 'episode-200'}

    def test_events_missing_title_or_start_skipped(self):
        s = make_scraper()
        s._get_json = MagicMock(return_value={'channels': [{'programs': [
            {'id': 'ok', 'start_datetime': '2024-01-15T06:00:00+01:00',
             'title': 'X'},
            {'id': 'no-start', 'title': 'Y'},
            {'id': 'no-title',
             'start_datetime': '2024-01-15T06:00:00+01:00'},
        ]}]})
        progs = s.fetch_programs(MagicMock(id='CH'), 'c',
                                 date(2024, 1, 15))
        assert len(progs) == 1


class TestSiteMeta:
    def test_site_name(self):
        assert make_scraper().site_name() == 'port.hu'

    def test_today_is_a_date(self):
        assert isinstance(make_scraper().today(), date)


class TestHttpHelpers:
    def test_bootstrap_once_then_requests(self):
        s = make_scraper()
        resp = MagicMock()
        resp.json.return_value = {'ok': True}
        s._http.get.return_value = resp
        assert s._get_json('http://x') == {'ok': True}
        s._get_text('http://y')
        urls = [c.args[0] for c in s._http.get.call_args_list]
        # landing page fetched once for the session cookie
        assert urls == ['https://port.hu', 'http://x', 'http://y']

    def test_get_text_returns_body(self):
        s = make_scraper()
        resp = MagicMock()
        resp.text = '<html/>'
        s._http.get.return_value = resp
        assert s._get_text('http://x') == '<html/>'
        resp.raise_for_status.assert_called()


class TestChannelMap:
    def test_map_built_and_cached(self, cache):
        s = make_scraper(cache=cache)
        s._get_json = MagicMock(return_value={'channels': [
            {'id': 'tvchannel-1', 'name': 'M1', 'logo': 'http://x/l.png'},
            {'id': 'tvchannel-2', 'name': 'M2'},  # no logo
        ]})
        cmap = s._channel_map()
        assert cmap['tvchannel-1'] == {'name': 'M1',
                                       'logo': 'http://x/l.png'}
        assert cmap['tvchannel-2'] == {'name': 'M2', 'logo': None}
        s._channel_map()  # second call served from cache
        s._get_json.assert_called_once()


class TestFetchChannel:
    def test_known_id_returns_channel(self, cache):
        s = make_scraper(cache=cache)
        s._get_json = MagicMock(return_value={'channels': [
            {'id': 'tvchannel-1', 'name': 'M1', 'logo': 'http://x/l.png'}]})
        ch = s.fetch_channel('tvchannel-1', None, 'M1')
        assert ch.id == 'TVCHANNEL-1.PORT.HU'
        assert ch.icon.src == 'http://x/l.png'
        # channel entry itself is cached - no second API call
        s.fetch_channel('tvchannel-1', None, 'M1')
        s._get_json.assert_called_once()

    def test_xmltv_id_overrides_generated_id(self):
        s = make_scraper()
        s._get_json = MagicMock(return_value={'channels': [
            {'id': 'tvchannel-1', 'name': 'M1'}]})
        ch = s.fetch_channel('tvchannel-1', 'M1HD', 'M1')
        assert ch.id == 'M1HD'

    def test_unknown_id_raises(self):
        s = make_scraper()
        s._get_json = MagicMock(return_value={'channels': []})
        with pytest.raises(requests.RequestException, match='unknown'):
            s.fetch_channel('tvchannel-999', None, 'X')


class TestGetProgramDetails:
    def test_no_film_url_returns_empty(self):
        assert make_scraper()._get_program_details('movie-1', None) == {}

    def test_cached_value_returned(self, cache):
        s = make_scraper(cache=cache)
        cache.set('program:port.hu:movie-1', {'desc': 'd'}, 'program')
        assert s._get_program_details('movie-1', '/film/x') == \
            {'desc': 'd'}

    def test_fetch_failure_returns_empty(self):
        s = make_scraper()
        s._get_text = MagicMock(
            side_effect=requests.ConnectionError('down'))
        assert s._get_program_details('movie-1', '/film/x') == {}

    def test_relative_url_resolved_and_cached(self, cache):
        s = make_scraper(cache=cache)
        s._get_text = MagicMock(
            return_value='<html></html>')  # no ld+json -> {}
        assert s._get_program_details('movie-1', '/film/x') == {}
        s._get_text.assert_called_once_with('https://port.hu/film/x')
        # the empty parse result is still cached - retried only on TTL
