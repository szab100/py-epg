#!/usr/bin/env python3
"""Tests for the tvmustra.hu scraper: the broadcast-day listing parser
(with midnight rollover), detail-payload mapping and programme assembly.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup
from xmltv.models import Programme, Title

from py_epg.scrapers.tvmustra_hu import TvMustraHu, RE_DETAILS_CALL


def make_scraper(cache=None, metadata=None):
    s = TvMustraHu(user_agent='test', cache=cache, metadata=metadata,
                   request_delay=0)
    s._http = MagicMock()
    return s


def make_programme():
    return Programme(channel='CH', start='20240115060000 +0100',
                     title=[Title(content=['Cím'], lang='hu')])


LISTING_HTML = '''
<div class="v-prog-container"
     onclick="loadDetailsVertical(this, 'tab1', 101, '23:50')">
  <div class="v-time">23:50</div><div class="v-title">Esti műsor</div>
</div>
<div class="v-prog-container"
     onclick="loadDetailsVertical(this, 'tab1', 102, '00:10')">
  <div class="v-time">00:10</div><div class="v-title">Hajnali műsor</div>
</div>
<div class="v-prog-container">
  <div class="v-time">01:00</div><div class="v-title">Nincs onclick</div>
</div>
'''


class TestDetailsCallRegex:
    def test_extracts_table_id_time(self):
        m = RE_DETAILS_CALL.search(
            "loadDetailsVertical(this, 'prog_table', 98765, '14:30')")
        assert m.groups() == ('prog_table', '98765', '14:30')

    def test_no_match(self):
        assert RE_DETAILS_CALL.search('otherFunc(1,2)') is None


class TestFetchListing:
    def test_midnight_rollover_and_malformed_skip(self):
        s = make_scraper()
        soup = BeautifulSoup(LISTING_HTML, 'html.parser')
        s._get_soup = MagicMock(return_value=soup)
        entries = s._fetch_listing('CHAN', date(2024, 1, 15))
        assert len(entries) == 2
        assert entries[0]['table'] == 'tab1'
        assert entries[0]['id'] == '101'
        assert entries[0]['title'] == 'Esti műsor'
        assert entries[0]['start'].startswith('20240115235000')
        # past-midnight program rolls into the next day
        assert entries[1]['start'].startswith('20240116001000')
        assert entries[1]['start'].endswith('+0100')


class TestParseProgramDetails:
    def test_full_payload(self):
        d = make_scraper()._parse_program_details({
            'kepek_list': ['/th_kepek/a.jpg'],
            'angolcim': 'Original Title', 'alcim': 'Ep cím',
            'tartalom': 'Leírás', 'rendezo': 'D1, D2',
            'szereplok': 'A1; A2', 'gyartasiev': '2021.',
            'kategoria': 'sorozat', 'gyartasio': 'magyar',
            'evad': '3', 'epizod': '7', 'hossz': '45',
            'kor': '12', 'ismetles': 1})
        assert d['icon'] == 'https://www.tvmustra.hu/kepek/a.jpg'
        assert d['orig_title'] == 'Original Title'
        assert d['sub_titles'] == ['Ep cím']
        assert d['descs'] == ['Leírás']
        assert d['directors'] == ['D1', 'D2']
        assert d['actors'] == ['A1', 'A2']
        assert d['date'] == '2021'
        assert d['category'] == 'sorozat'
        assert d['country'] == 'magyar'
        assert d['season'] == 3
        assert d['episode'] == 7
        assert d['length_min'] == '45'
        assert d['age_rating'] == '12'
        assert d['previously_shown'] is True

    @pytest.mark.parametrize('hossz,expected', [
        ('90', '90'),            # bare minutes
        ('01:30:00', '90'),      # HH:MM:SS
        ('01:30:40', '91'),      # seconds >= 30 round up
        ('01:30:10', '90'),
        ('', None),
    ])
    def test_hossz_normalisation(self, hossz, expected):
        d = make_scraper()._parse_program_details({'hossz': hossz})
        assert d['length_min'] == expected

    @pytest.mark.parametrize('gyartasiev,expected', [
        ('2021', '2021'), ('2021.', '2021'), ('2021-2022', '2021'),
        ('', None),
    ])
    def test_year_normalisation(self, gyartasiev, expected):
        d = make_scraper()._parse_program_details({'gyartasiev': gyartasiev})
        assert d['date'] == expected

    def test_non_numeric_season_episode(self):
        d = make_scraper()._parse_program_details(
            {'evad': 'x', 'epizod': 'y'})
        assert d['season'] is None
        assert d['episode'] is None


class TestApplyProgramDetails:
    def test_meta_lookup_tagged(self):
        s = make_scraper(metadata=MagicMock())
        p = make_programme()
        s._apply_program_details(p, {
            'orig_title': 'Orig', 'date': '2021', 'season': 3,
            'episode': 7, 'icon': None, 'sub_titles': [], 'descs': [],
            'directors': [], 'actors': [], 'category': None,
            'country': None, 'length_min': None, 'age_rating': None,
            'previously_shown': False}, 'Magyar Cím')
        assert p._meta_lookup == ('Magyar Cím', 'Orig', '2021', 3, 7)

    def test_episode_num_and_fields(self):
        s = make_scraper()
        p = make_programme()
        s._apply_program_details(p, {
            'orig_title': 'Orig', 'sub_titles': ['alcim'],
            'descs': ['d'], 'directors': ['D'], 'actors': ['A'],
            'date': '2001', 'category': 'dráma', 'country': 'magyar',
            'season': 3, 'episode': 7, 'length_min': '45',
            'age_rating': '12', 'previously_shown': True,
            'icon': 'http://x/i.jpg'})
        nums = {n.system: n.content[0] for n in p.episode_num}
        assert nums['onscreen'] == 'S03E07'
        assert nums['xmltv_ns'] == '2.6.'
        assert p.rating[0].value == '12'
        assert p.previously_shown is not None
        assert p.icon[0].src == 'http://x/i.jpg'
        assert p.sub_title[0].content[0] == 'alcim'

    def test_falsy_details_still_tags_and_sets_icon(self):
        s = make_scraper(metadata=MagicMock())
        p = make_programme()
        s._apply_program_details(p, {
            'icon': 'http://x/f.jpg', 'orig_title': None,
            'sub_titles': [], 'descs': [], 'directors': [],
            'actors': [], 'date': None, 'category': None,
            'country': None, 'season': None, 'episode': None,
            'length_min': None, 'age_rating': None,
            'previously_shown': False}, 'Cím')
        assert p.icon[0].src == 'http://x/f.jpg'
        assert p._meta_lookup == ('Cím', None, None, None, None)


class TestGetProgramDetails:
    def test_non_success_status_returns_empty(self):
        s = make_scraper()
        s._get_json = MagicMock(return_value={'status': 'error'})
        assert s._get_program_details('tab', '1') == {}

    def test_result_cached_by_table_and_id(self, cache):
        s = make_scraper(cache=cache)
        s._get_json = MagicMock(return_value={
            'status': 'success', 'data': {'alcim': 'x'}})
        d1 = s._get_program_details('tab', '1')
        d2 = s._get_program_details('tab', '1')
        assert d1 == d2
        s._get_json.assert_called_once()

    def test_request_error_returns_empty(self):
        import requests
        s = make_scraper()
        s._get_json = MagicMock(
            side_effect=requests.ConnectionError('down'))
        assert s._get_program_details('tab', '1') == {}


class TestSiteMeta:
    def test_site_name(self):
        assert make_scraper().site_name() == 'tvmustra.hu'

    def test_today_is_a_date(self):
        assert isinstance(make_scraper().today(), date)


class TestFetchChannel:
    def test_logo_extracted_and_cached(self, cache):
        s = make_scraper(cache=cache)
        s._get_soup = MagicMock(return_value=BeautifulSoup(
            '<div class="ch-logo-white-bg"><img src="/logo.png"/></div>',
            'html.parser'))
        ch = s.fetch_channel('MR1KOSSUTH', None, 'Kossuth')
        assert ch.id == 'MR1KOSSUTH.TVMUSTRA.HU'
        assert ch.icon.src == 'https://www.tvmustra.hu/logo.png'
        s.fetch_channel('MR1KOSSUTH', None, 'Kossuth')   # cached
        s._get_soup.assert_called_once()

    def test_xmltv_id_overrides_generated_id(self):
        s = make_scraper()
        s._get_soup = MagicMock(
            return_value=BeautifulSoup('<div/>', 'html.parser'))
        ch = s.fetch_channel('MR1KOSSUTH', 'KOSSUTH.RADIO', 'Kossuth')
        assert ch.id == 'KOSSUTH.RADIO'

    def test_missing_logo_leaves_icon_none(self):
        s = make_scraper()
        s._get_soup = MagicMock(
            return_value=BeautifulSoup('<div/>', 'html.parser'))
        ch = s.fetch_channel('X', None, 'n')
        assert ch.icon is None


class TestFetchPrograms:
    DETAILS = {
        'icon': None, 'orig_title': None, 'sub_titles': [], 'descs': [],
        'directors': [], 'actors': [], 'date': '2020',
        'category': None, 'country': None, 'season': None,
        'episode': None, 'length_min': None, 'age_rating': None,
        'previously_shown': False}

    def test_cached_listing_builds_programs(self, cache):
        s = make_scraper(cache=cache)
        entries = [
            {'table': 't', 'id': '1', 'title': 'A',
             'start': '20240115060000 +0100'},
            {'table': 't', 'id': '2', 'title': 'B',
             'start': '20240115070000 +0100'},
        ]
        cache.set('listing:tvmustra.hu:CH:2024-01-15', entries, 'listing')
        s._get_program_details = MagicMock(
            side_effect=[dict(self.DETAILS), {}])
        channel = MagicMock(id='CH.TVMUSTRA.HU')
        progs = s.fetch_programs(channel, 'CH', date(2024, 1, 15))
        assert [p.title[0].content[0] for p in progs] == ['A', 'B']
        # one missing detail -> logged, programme kept with listing data
        assert progs[0].date == '2020'
        assert progs[1].date is None
        s._get_soup = MagicMock()
        assert not s._get_soup.called    # listing came from cache

    def test_uncached_listing_fetched_then_cached(self, cache):
        s = make_scraper(cache=cache)
        s._fetch_listing = MagicMock(return_value=[
            {'table': 't', 'id': '1', 'title': 'A',
             'start': '20240115060000 +0100'}])
        s._get_program_details = MagicMock(return_value={})
        channel = MagicMock(id='CH.TVMUSTRA.HU')
        s.fetch_programs(channel, 'CH', date(2024, 1, 15))
        s.fetch_programs(channel, 'CH', date(2024, 1, 15))
        s._fetch_listing.assert_called_once()  # second run from cache


class TestAbsUrl:
    @pytest.mark.parametrize('src,expected', [
        (None, None),
        ('', None),
        ('/x.png', 'https://www.tvmustra.hu/x.png'),
        ('http://cdn/x.png', 'http://cdn/x.png'),
        ('relative.png', None),
    ])
    def test_abs_url(self, src, expected):
        assert make_scraper()._abs_url(src) == expected


class TestHttpHelpers:
    def test_get_soup(self):
        s = make_scraper()
        resp = MagicMock()
        resp.text = '<div class="x">t</div>'
        s._http.get.return_value = resp
        soup_result = s._get_soup('http://x')
        assert soup_result.select_one('div.x').text == 't'
        resp.raise_for_status.assert_called_once()

    def test_get_json(self):
        s = make_scraper()
        resp = MagicMock()
        resp.json.return_value = {'ok': 1}
        s._http.get.return_value = resp
        assert s._get_json('http://x') == {'ok': 1}
        resp.raise_for_status.assert_called_once()
