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

    def test_single_paragraph_block_is_desc_only(self):
        s = make_scraper()
        details = {'icon': None, 'orig_title': None, 'sub_titles': [],
                   'descs': [], 'directors': [], 'actors': []}
        s._parse_mixed_description(
            details, 'Csak egy leírás.\nRendezte: Jane Doe')
        # no blank-line split -> the whole block is the description
        assert details['descs'] == ['Csak egy leírás.']
        assert details['sub_titles'] == []

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


class TestSiteMeta:
    def test_site_name(self):
        assert make_scraper().site_name() == 'm.musor.tv'

    def test_today_is_a_date(self):
        from datetime import date
        assert isinstance(make_scraper().today(), date)


class TestFetchChannel:
    def test_logo_extracted_and_cached(self, cache):
        s = make_scraper(cache=cache)
        s._get_soup = MagicMock(return_value=soup(
            '<img class="channelheaderlink" src="/logo.png"/>'))
        ch = s.fetch_channel('m1', 'M1')
        assert ch.id == 'M1.M.MUSOR.TV'
        assert ch.icon.src == 'https://m.musor.tv/logo.png'
        s.fetch_channel('m1', 'M1')  # cached - no second fetch
        s._get_soup.assert_called_once()

    def test_missing_logo_leaves_icon_none(self):
        s = make_scraper()
        s._get_soup = MagicMock(return_value=soup('<div/>'))
        assert s.fetch_channel('m1', 'M1').icon is None


LISTING_HTML = '''
<section itemscope>
  <h3 class="wideprogentry_progtitle"><a href="/prg/1">
    <span itemprop="name">Reggeli műsor</span></a></h3>
  <div itemprop="description">Hírek, 2020</div>
  <span itemprop="startDate" content="2024-01-15GMT05:00:00"/>
</section>
<section itemscope>
  <h3 class="wideprogentry_progtitle"><a href="/prg/2">
    <span itemprop="name">Tegnapi műsor</span></a></h3>
  <span itemprop="startDate" content="2024-01-14GMT10:00:00"/>
</section>
'''

EMPTY_DETAILS = {'icon': None, 'orig_title': None, 'sub_titles': [],
                 'descs': [], 'directors': [], 'actors': []}


class TestFetchPrograms:
    def test_programs_built_off_date_skipped(self):
        from datetime import date as date_cls
        s = make_scraper()
        s._get_soup = MagicMock(return_value=soup(LISTING_HTML))
        s._get_program_details = MagicMock(return_value=dict(EMPTY_DETAILS))
        channel = MagicMock(id='CH')
        progs = s.fetch_programs(channel, 'ch', date_cls(2024, 1, 15))
        # the second entry (10:00 GMT = 11:00 local on 01-14) is off the
        # requested broadcast day and skipped
        assert [p.title[0].content[0] for p in progs] == ['Reggeli műsor']
        assert progs[0].start == '20240115060000 +0100'
        assert progs[0].date == '2020'   # from the description field
        s._get_program_details.assert_called_once()


class TestParseProgramDetails:
    def test_icon_and_mixed_description(self):
        s = make_scraper()
        page = soup(
            '<html><img itemprop="image" src="/i.jpg"/>'
            '<div class="eventinfolongdescinner">'
            '(Original)\n\nAlcím\n\nLeírás.'
            '</div></html>')
        d = s._parse_program_details(page)
        assert d['icon'] == 'https://m.musor.tv/i.jpg'
        assert d['orig_title'] == 'Original'
        assert d['sub_titles'] == ['Alcím']
        assert d['descs'] == ['Leírás.']


class TestApplyProgramDetails:
    def test_all_fields_applied(self):
        s = make_scraper()
        p = make_programme()
        s._apply_program_details(p, {
            'icon': 'http://x/i.jpg', 'orig_title': 'Orig',
            'sub_titles': ['sub'], 'descs': ['desc'],
            'directors': ['D'], 'actors': ['A']})
        assert p.icon[0].src == 'http://x/i.jpg'
        assert p.title[1].content[0] == 'Orig'
        assert p.sub_title[0].content[0] == 'sub'
        assert p.desc[0].content[0] == 'desc'
        assert p.credits.director == ['D']
        assert p.credits.actor[0].content[0] == 'A'


class TestGetSoup:
    def test_fetches_and_parses(self):
        s = make_scraper()
        resp = MagicMock()
        resp.text = '<div class="x">t</div>'
        s._http.get.return_value = resp
        assert s._get_soup('http://x').select_one('div.x').text == 't'
        resp.raise_for_status.assert_called_once()
