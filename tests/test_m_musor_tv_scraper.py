#!/usr/bin/env python3
"""Tests for the m.musor.tv scraper: title-embedded season/episode
parsing, subtitle/year extraction and the mixed free-text description
parser."""

from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup
from xmltv.models import Programme, Title

from py_epg.scrapers.m_musor_tv import MusorTvMobile


def make_scraper(cache=None, metadata=None):
    s = MusorTvMobile(user_agent='test', cache=cache, metadata=metadata,
                      request_delay=0)
    s._http = MagicMock()
    return s


def make_programme():
    return Programme(channel='CH', title=[Title(content=['Cím'])],
                     clumpidx=None)


def soup(html):
    return BeautifulSoup(html, 'html.parser')


class TestSetPrgEpisodeInfo:
    @pytest.mark.parametrize('marker,onscreen', [
        ('IV./12.', 'S04E12'),   # subtractive numeral, was parsed as 5
        ('IX./5.', 'S09E05'),
        ('XIV./2.', 'S14E02'),
        ('III./7.', 'S03E07'),
        ('I./1.', 'S01E01'),
    ])
    def test_roman_season_and_episode(self, marker, onscreen):
        s = make_scraper()
        p = make_programme()
        s._set_prg_episode_info(p, f'Bogyó és Babóca {marker} rész')
        nums = {n.system: n.content[0] for n in p.episode_num}
        assert nums['onscreen'] == onscreen
        # episode marker is stripped from the title
        assert p.title[0].content[0] == 'Bogyó és Babóca'

    def test_single_episode_number(self):
        s = make_scraper()
        p = make_programme()
        s._set_prg_episode_info(p, 'Műsor 5.')
        nums = {n.system: n.content[0] for n in p.episode_num}
        assert nums['onscreen'] == 'S--E05'
        assert nums['xmltv_ns'] == '.4.'

    def test_no_episode_info_untouched(self):
        s = make_scraper()
        p = make_programme()
        s._set_prg_episode_info(p, 'Egyszerű film')
        assert not p.episode_num
        assert p.title[0].content[0] == 'Cím'


class TestSetPrgSubTitleAndYear:
    def _prg(self, description_html):
        prg = soup(f'<section><div itemprop="description">'
                   f'{description_html}</div></section>')
        p = make_programme()
        make_scraper()._set_prg_sub_title_and_year(p, prg)
        return p

    def test_subtitle_and_year_range(self):
        p = self._prg('Alcím szöveg, 2005-2010')
        assert p.date == '2010'   # end of the range
        assert p.sub_title[0].content[0] == 'Alcím szöveg'

    def test_subtitle_and_year(self):
        p = self._prg('Alcím, 1999')
        assert p.date == '1999'
        assert p.sub_title[0].content[0] == 'Alcím'

    def test_bare_year_becomes_date(self):
        p = self._prg('2015')
        assert p.date == '2015'
        assert not p.sub_title

    def test_plain_subtitle(self):
        p = self._prg('Csak alcím')
        assert p.sub_title[0].content[0] == 'Csak alcím'
        assert not p.date

    def test_missing_element(self):
        p = make_programme()
        make_scraper()._set_prg_sub_title_and_year(
            p, soup('<section></section>'))
        assert not p.sub_title
        assert not p.date


class TestSetPrgStart:
    def test_gmt_converted_to_budapest(self):
        s = make_scraper()
        prg = soup('<section><span itemprop="startDate" '
                   'content="2024-01-15GMT14:30:00"/></section>')
        p = make_programme()
        start = s._set_prg_start(p, prg)
        # content is UTC; Budapest is UTC+1 in January
        assert p.start == '20240115153000 +0100'
        assert start.date().isoformat() == '2024-01-15'


class TestParseMixedDescription:
    def test_full_block(self):
        s = make_scraper()
        details = {'icon': None, 'orig_title': None, 'sub_titles': [],
                   'descs': [], 'directors': [], 'actors': []}
        s._parse_mixed_description(
            details,
            '(The Original)\n\n'
            'Epizód alcím\n\n'
            'Hosszabb leírás szövege.\n'
            'Rendezte: Jane Doe\n'
            'Főszereplők: John Smith, Jane Roe')
        assert details['orig_title'] == 'The Original'
        assert details['sub_titles'] == ['Epizód alcím']
        assert details['descs'] == ['Hosszabb leírás szövege.']
        assert details['directors'] == ['Jane Doe']
        assert details['actors'] == ['John Smith', 'Jane Roe']

    def test_single_line_is_desc(self):
        s = make_scraper()
        details = {'icon': None, 'orig_title': None, 'sub_titles': [],
                   'descs': [], 'directors': [], 'actors': []}
        s._parse_mixed_description(details, 'Sima leírás.')
        assert details['descs'] == ['Sima leírás.']

    def test_empty_is_noop(self):
        s = make_scraper()
        details = {'descs': []}
        s._parse_mixed_description(details, '')
        assert details['descs'] == []

    def test_description_only_multiline(self):
        s = make_scraper()
        details = {'icon': None, 'orig_title': None, 'sub_titles': [],
                   'descs': [], 'directors': [], 'actors': []}
        s._parse_mixed_description(
            details, 'Első sor\n\nMásodik bekezdés.')
        # subtitle slot + remaining desc
        assert details['sub_titles'] == ['Első sor']
        assert details['descs'] == ['Második bekezdés.']


class TestGetProgramDetails:
    def test_cached_by_url(self, cache):
        s = make_scraper(cache=cache)
        s._get_soup = MagicMock(return_value=soup(
            '<html><img itemprop="image" src="/i.jpg"/></html>'))
        d1 = s._get_program_details('http://x/prg')
        d2 = s._get_program_details('http://x/prg')
        assert d1 == d2 == {'icon': 'https://m.musor.tv/i.jpg',
                            'orig_title': None, 'sub_titles': [],
                            'descs': [], 'directors': [], 'actors': []}
        s._get_soup.assert_called_once()

    def test_fetch_failure_returns_empty_details(self):
        import requests
        s = make_scraper()
        s._get_soup = MagicMock(
            side_effect=requests.ConnectionError('down'))
        d = s._get_program_details('http://x/prg')
        assert d['icon'] is None and d['descs'] == []
