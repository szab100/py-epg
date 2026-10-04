#!/usr/bin/env python3
"""Tests for the port.hu suggest-list provider: matching rules and the
'subtitle' field parser (countries/genres/year)."""

from unittest.mock import MagicMock

import pytest

from py_epg.metadata_providers.porthu import PortHuMetadata
from conftest import cfg_el, json_response


def make_provider(**kwargs):
    cfg = cfg_el('<metadata provider="porthu"/>')
    p = PortHuMetadata(cfg, session=MagicMock(), **kwargs)
    p._delay = 0          # no sleeping in tests
    p._bootstrapped = True  # skip the session-token bootstrap request
    return p


class TestCacheKey:
    def test_episode_independent(self):
        p = make_provider()
        k1 = p._cache_key('Show', 'Orig', '2010', 1, 2)
        k2 = p._cache_key('Show', 'Orig', '2010', 5, 9)
        assert k1 == k2  # port.hu has no per-episode artwork


class TestParseSubtitle:
    @pytest.mark.parametrize('subtitle,countries,genres,year', [
        ('amerikai-német sci-fi sorozat, 1987',
         ['amerikai', 'német'], ['sci-fi'], '1987'),
        ('magyar romantikus film, 2005', ['magyar'],
         ['romantikus'], '2005'),
        # 'sci-fi' must NOT be split into countries even though '-'
        ('amerikai sci-fi, 1999', ['amerikai'], ['sci-fi'], '1999'),
        ('brit krimi minisorozat, 2015', ['brit'], ['krimi'], '2015'),
        # no year
        ('magyar sorozat', ['magyar'], [], None),
        # empty
        ('', [], [], None),
        (None, [], [], None),
    ])
    def test_parse(self, subtitle, countries, genres, year):
        assert PortHuMetadata._parse_subtitle(subtitle) == \
            (countries, genres, year)


class TestBestMatch:
    def setup_method(self):
        self.p = make_provider()

    def hit(self, name='Bogyó és Babóca',
            subtitle='magyar animációs sorozat, 2010'):
        return {'name': name, 'subtitle': subtitle}

    def test_exact_title_match(self):
        assert self.p._best_match(
            [self.hit()], 'Bogyó és Babóca', None, '2010') is not None

    def test_normalised_title_match(self):
        res = self.hit(name='Bogyo es Baboca')
        assert self.p._best_match([res], 'Bogyó és Babóca', None,
                                  '2010') is res

    def test_wrong_title_rejected(self):
        assert self.p._best_match(
            [self.hit(name='Más műsor')], 'Bogyó és Babóca', None,
            '2010') is None

    def test_year_within_one(self):
        res = self.hit()
        assert self.p._best_match([res], 'Bogyó és Babóca', None,
                                  '2011') is res
        assert self.p._best_match([res], 'Bogyó és Babóca', None,
                                  '2015') is None

    def test_missing_year_rejected_when_year_known(self):
        res = self.hit(subtitle='magyar sorozat')
        assert self.p._best_match([res], 'Bogyó és Babóca', None,
                                  '2010') is None
        # but fine when no year is known
        assert self.p._best_match([res], 'Bogyó és Babóca', None,
                                  None) is res

    def test_matches_on_orig_title(self):
        res = self.hit(name='Berry and Dolly')
        assert self.p._best_match(
            [res], 'Bogyó és Babóca', 'Berry and Dolly', '2010') is res


class TestLookup:
    def test_result_fields(self):
        p = make_provider()
        p._http.get.return_value = json_response([{
            'name': 'Bogyó és Babóca',
            'subtitle': 'magyar animációs sorozat, 2010',
            'thumbnail': 'http://x/p.jpg'}])
        meta = p.metadata_for('Bogyó és Babóca', year='2010')
        assert meta['title'] == 'Bogyó és Babóca'
        assert meta['icon'] == 'http://x/p.jpg'
        assert meta['countries'] == ['magyar']
        assert meta['genres'] == ['animációs']
        assert meta['year'] == '2010'
        assert meta['_src'] == 'porthu'

    def test_non_json_response_treated_as_no_results(self):
        p = make_provider()
        r = MagicMock()
        r.json.side_effect = ValueError('not json')
        r.raise_for_status.return_value = None
        p._http.get.return_value = r
        assert p.metadata_for('Show') == {}

    def test_http_error_propagates(self):
        import requests
        p = make_provider()
        p._http.get.side_effect = requests.ConnectionError('boom')
        with pytest.raises(requests.ConnectionError):
            p.metadata_for('Show')
