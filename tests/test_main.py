#!/usr/bin/env python3
"""Tests for main.py: the batched metadata pass on the worker pool,
per-programme field application and stop-time synthesis."""

import logging
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from lxml import etree as ET
from xmltv.models import (Category, Channel, Country, Desc, DisplayName,
                          Icon, Programme, Rating, SubTitle, Title)

from py_epg.common.cache import Cache
from py_epg.common.proxy import ProxyPool
from py_epg.common.types import ChannelKey
from py_epg.main import (PyEPG, _init_worker, _remove_console_handler,
                         main)


def make_app(metadata=None, pool_size=1):
    """A PyEPG instance without __init__ (which needs argv/config)."""
    app = PyEPG.__new__(PyEPG)
    app._log = logging.getLogger('test')
    app._args = SimpleNamespace(progress_bar=False, quiet=True)
    app._metadata = metadata
    app._pool_size = pool_size
    app._cache = Cache(enabled=False)

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


def cfg_xml(body):
    return ET.ElementTree(ET.fromstring(f'<settings>{body}</settings>'))


def chan_el(site='site.hu', site_id='a', name='Chan', xmltv_id='x'):
    return ET.fromstring(
        f'<channel site="{site}" site_id="{site_id}" '
        f'xmltv_id="{xmltv_id}">{name}</channel>')


def fake_scraper(channel_id='A.SITE.HU', programmes_per_day=1):
    s = MagicMock()
    s.site_name.return_value = 'site.hu'
    s.fetch_channel.side_effect = lambda sid, xid, name: Channel(
        id=f'{sid}.{channel_id.split(".", 1)[-1]}'.upper(),
        display_name=[DisplayName(content=[name])])
    s.today.return_value = date(2024, 1, 15)
    s.fetch_programs.side_effect = lambda c, sid, d: [
        prog(start=d.strftime('%Y%m%d') + '060000 +0100')
        for _ in range(programmes_per_day)]
    return s


def patch_worker_identity(monkeypatch):
    """_fetch_channel reads process identity for the progress bar."""
    monkeypatch.setattr(
        'py_epg.main.current_process',
        lambda: SimpleNamespace(_identity=(0,)))


class TestConsoleHandlers:
    def test_removes_stream_but_keeps_file(self, tmp_path):
        logger = logging.getLogger('handler-test')
        stream = logging.StreamHandler()
        file_h = logging.FileHandler(tmp_path / 'x.log')
        logger.addHandler(stream)
        logger.addHandler(file_h)
        try:
            _remove_console_handler(logger)
            assert stream not in logger.handlers
            assert file_h in logger.handlers
        finally:
            logger.removeHandler(file_h)
            file_h.close()

    def test_init_worker_disabled_is_noop(self):
        before = list(logging.getLogger().handlers)
        _init_worker(False)
        assert logging.getLogger().handlers == before


class TestParseArgs:
    def parse(self, argv):
        import unittest.mock
        with unittest.mock.patch.object(sys, 'argv', argv):
            return PyEPG._parse_args()

    def test_requires_config_or_stats(self):
        with pytest.raises(SystemExit):
            self.parse(['epg'])

    def test_stats_mode(self):
        args = self.parse(['epg', '--stats', 'epg.xml'])
        assert args.stats == 'epg.xml'
        assert args.config is None

    def test_quiet_disables_progress_bar(self):
        args = self.parse(['epg', '-c', 'c.xml', '-q'])
        assert args.quiet and args.progress_bar is False

    def test_json_flag(self):
        args = self.parse(['epg', '--stats', 'e.xml', '--json'])
        assert args.json is True


class TestInit:
    def test_full_init(self, tmp_path, monkeypatch):
        cfg = tmp_path / 'py_epg.xml'
        cfg.write_text(
            '<settings><filename>out.xml</filename>'
            '<user-agent>test</user-agent></settings>')
        monkeypatch.setattr(sys, 'argv', ['epg', '-c', str(cfg)])
        monkeypatch.setattr('py_epg.main.Pool',
                            lambda *a, **k: MagicMock())
        app = PyEPG()
        assert app._pool_size == 1
        assert set(app._epg_scrapers) == {
            'port.hu', 'tvmustra.hu', 'm.musor.tv'}

    def test_pool_size_from_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / 'py_epg.xml'
        cfg.write_text(
            '<settings><filename>o.xml</filename><pool-size>7</pool-size>'
            '<user-agent>t</user-agent></settings>')
        monkeypatch.setattr(sys, 'argv', ['epg', '-c', str(cfg)])
        monkeypatch.setattr('py_epg.main.Pool',
                            lambda *a, **k: MagicMock())
        assert PyEPG()._pool_size == 7


class TestBuildProxy:
    def app(self, body):
        app = make_app()
        app._config = cfg_xml(body)
        app._cache = MagicMock()
        return app

    def test_no_proxy_returns_none(self):
        assert self.app('<x/>')._build_proxy() is None

    def test_static_proxy_returns_url(self):
        assert self.app('<proxy>http://h:1</proxy>')._build_proxy() == \
            'http://h:1'

    def test_proxy_list_builds_pool(self):
        pool = self.app(
            '<proxy-list cooldown="60">'
            '<proxy>http://a:1</proxy>'
            '</proxy-list>')._build_proxy()
        assert isinstance(pool, ProxyPool)
        assert 'http://a:1' in pool._proxies

    def test_proxy_list_warmed_in_parent(self):
        """The list is fetched once here so pickled worker copies
        inherit it instead of all refreshing simultaneously."""
        resp = MagicMock()
        resp.text = '1.1.1.1:1\n2.2.2.2:2'
        resp.raise_for_status.return_value = None
        with patch('py_epg.common.proxy.requests.get',
                   return_value=resp) as g:
            pool = self.app(
                '<proxy-list url="http://list"/>')._build_proxy()
        assert g.call_count == 1
        assert len(pool._proxies) == 2


class TestBuildCache:
    def test_disabled_without_element(self):
        app = make_app()
        app._config = cfg_xml('<x/>')
        assert app._build_cache()._enabled is False

    def test_disabled_by_attribute(self):
        app = make_app()
        app._config = cfg_xml('<cache enabled="false"/>')
        assert app._build_cache()._enabled is False

    def test_enabled_with_ttls(self, tmp_path):
        app = make_app()
        db = tmp_path / 'c.sqlite'
        app._config = cfg_xml(
            f'<cache file="{db}" enabled="true" '
            'listing-ttl="10" meta-ttl="20"/>')
        c = app._build_cache()
        assert c._enabled is True
        assert c._ttls['listing'] == 10 and c._ttls['meta'] == 20
        c.close()


class TestBuildMetadata:
    def test_no_metadata_element_returns_none(self):
        app = make_app()
        app._config = cfg_xml('<x/>')
        app._proxy = None
        app._cache = MagicMock()
        assert app._build_metadata() is None

    def test_workers_attribute_warns_and_ignored(self, caplog):
        app = make_app()
        app._config = cfg_xml('<metadata workers="9" provider="x"/>')
        app._proxy = None
        app._cache = MagicMock()
        with caplog.at_level(logging.WARNING):
            assert app._build_metadata() is None  # unknown provider
        assert "'workers' attribute is ignored" in caplog.text

    def test_proxy_pool_builds_rotating_session(self):
        from py_epg.common.proxy import RotatingProxySession
        app = make_app()
        app._config = cfg_xml('<metadata provider="bogus"/>')
        app._proxy = ProxyPool()
        app._cache = MagicMock()
        assert app._build_metadata() is None


class TestRequestDelay:
    def test_absent_defaults_zero(self):
        app = make_app()
        app._config = cfg_xml('<x/>')
        assert app._request_delay() == 0.0

    def test_reads_element(self):
        app = make_app()
        app._config = cfg_xml('<request-delay>2.5</request-delay>')
        assert app._request_delay() == 2.5


class TestInitEpgScrapers:
    def test_all_sites_registered(self):
        app = make_app()
        app._config = cfg_xml('<user-agent>t</user-agent>')
        app._proxy = None
        app._cache = MagicMock()
        scrapers = app._init_epg_scrapers()
        assert set(scrapers) == {'port.hu', 'tvmustra.hu', 'm.musor.tv'}


class TestFetchChannel:
    def test_fetches_each_day(self, monkeypatch):
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml('<timespan>2</timespan>')
        scraper = fake_scraper()
        app._epg_scrapers = {'site.hu': scraper}
        key, programs = app._fetch_channel(chan_el())
        assert key.id == 'A.SITE.HU'
        assert len(programs) == 2
        assert scraper.fetch_programs.call_count == 2

    def test_channel_fetch_error_returns_empty(self, monkeypatch):
        import requests
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml('<timespan>1</timespan>')
        scraper = fake_scraper()
        scraper.fetch_channel.side_effect = requests.ConnectionError('x')
        app._epg_scrapers = {'site.hu': scraper}
        key, programs = app._fetch_channel(chan_el(site_id='a'))
        assert key.id == 'A'          # upper-cased site id
        assert key.channel is None
        assert programs == []

    def test_day_failure_skipped_others_kept(self, monkeypatch):
        import requests
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml('<timespan>2</timespan>')
        scraper = fake_scraper()
        scraper.fetch_programs.side_effect = [
            requests.ConnectionError('down'), [prog()]]
        app._epg_scrapers = {'site.hu': scraper}
        _, programs = app._fetch_channel(chan_el())
        assert len(programs) == 1     # failed day skipped, not fatal

    def test_xmltv_id_passed_to_scraper(self, monkeypatch):
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml('<timespan>1</timespan>')
        scraper = fake_scraper()
        app._epg_scrapers = {'site.hu': scraper}
        app._fetch_channel(chan_el(xmltv_id='MY.STABLE.ID'))
        scraper.fetch_channel.assert_called_once_with(
            'a', 'MY.STABLE.ID', 'Chan')

    def test_unknown_site_raises(self, monkeypatch):
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml('<timespan>1</timespan>')
        app._epg_scrapers = {}
        with pytest.raises(RuntimeError, match='scraper'):
            app._fetch_channel(chan_el())


class TestFetchData:
    def test_collects_per_channel(self, monkeypatch):
        patch_worker_identity(monkeypatch)
        app = make_app()
        app._config = cfg_xml(
            '<timespan>1</timespan>'
            '<channel site="site.hu" site_id="a" xmltv_id="x">A</channel>'
            '<channel site="site.hu" site_id="b" xmltv_id="y">B</channel>')
        scraper = fake_scraper()
        app._epg_scrapers = {'site.hu': scraper}
        data = app._fetch_data()
        assert len(data) == 2
        assert all(len(v) == 1 for v in data.values())

    def test_missing_site_raises(self):
        app = make_app()
        app._config = cfg_xml(
            '<channel site="nowhere.tv" site_id="a" xmltv_id="x">A</channel>')
        app._epg_scrapers = {'site.hu': MagicMock()}
        with pytest.raises(RuntimeError, match='nowhere.tv'):
            app._fetch_data()


class TestBuildXmltv:
    def test_channels_sorted_and_programs_processed(self):
        app = make_app()
        c1 = Channel(id='B.HU', display_name=[DisplayName(content=['b'])])
        c2 = Channel(id='A.HU', display_name=[DisplayName(content=['a'])])
        p1 = prog(start='20240115070000 +0100')
        p2 = prog(start='20240115060000 +0100')
        data = {ChannelKey('B.HU', c1): [p1],
                ChannelKey('A.HU', c2): [p2]}
        tv, programs = app._build_xmltv(data)
        assert [c.id for c in tv.channel] == ['A.HU', 'B.HU']
        assert programs[0].start < programs[1].start
        # stop synthesized for the trailing programme
        assert p1.stop.endswith('235959 +0100')

    def test_channel_without_channel_object_omitted(self):
        app = make_app()
        data = {ChannelKey('GONE', None): []}
        tv, _ = app._build_xmltv(data)
        assert not tv.channel


class TestWriteXmltv:
    def test_writes_declared_xml(self, tmp_path):
        app = make_app()
        out = tmp_path / 'epg.xml'
        app._config = cfg_xml(f'<filename>{out}</filename>')
        from xmltv.models import Tv
        app._write_xmltv(Tv([], []))
        assert out.read_text().startswith('<?xml version="1.0"')


class TestRun:
    def _configured_app(self, tmp_path, monkeypatch, **kwargs):
        patch_worker_identity(monkeypatch)
        app = make_app(**kwargs)
        out = tmp_path / 'epg.xml'
        app._config = cfg_xml(
            f'<filename>{out}</filename><timespan>1</timespan>'
            '<channel site="site.hu" site_id="a" xmltv_id="x">A</channel>')
        app._epg_scrapers = {'site.hu': fake_scraper()}
        app._proxy = None
        return app, out

    def test_writes_file_without_metadata(self, tmp_path, monkeypatch):
        app, out = self._configured_app(tmp_path, monkeypatch)
        app.run()
        text = out.read_text()
        assert 'A.SITE.HU' in text

    def test_metadata_pass_and_proxy_stats(self, tmp_path, monkeypatch):
        app, out = self._configured_app(
            tmp_path, monkeypatch, metadata=MagicMock())
        app._proxy = ProxyPool()
        app.run()
        assert out.exists()
        app._metadata.metadata_for.assert_not_called()  # no _meta_lookup

    def test_metadata_failure_is_logged_not_fatal(
            self, tmp_path, monkeypatch):
        app, out = self._configured_app(
            tmp_path, monkeypatch, metadata=MagicMock())
        app._apply_metadata = MagicMock(
            side_effect=RuntimeError('meta exploded'))
        app.run()                      # must not raise
        assert out.exists()            # the pre-enrichment file survives


class TestMain:
    def test_stats_mode_does_not_need_config(
            self, tmp_path, monkeypatch, capsys):
        epg = tmp_path / 'e.xml'
        epg.write_text('<tv><channel id="X"><display-name>x</display-name>'
                       '</channel></tv>')
        monkeypatch.setattr(sys, 'argv', ['epg', '--stats', str(epg)])
        main()
        assert 'CHANNELS' in capsys.readouterr().out

    def test_proxy_stats_mode(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / 'cache.sqlite'
        Cache(str(db)).record_proxy_result('1.2.3.4:8080', ok=True)
        monkeypatch.setattr(
            sys, 'argv', ['epg', '--proxy-stats', str(db)])
        main()
        assert '1.2.3.4:8080' in capsys.readouterr().out

    def test_proxy_stats_default_db_from_config(
            self, tmp_path, monkeypatch, capsys):
        db = tmp_path / 'cache.sqlite'
        Cache(str(db)).record_proxy_result('9.9.9.9:1', ok=False)
        cfg = tmp_path / 'c.xml'
        cfg.write_text(
            f'<config><cache file="{db}"/></config>')
        monkeypatch.setattr(
            sys, 'argv', ['epg', '-c', str(cfg), '--proxy-stats'])
        main()
        out = capsys.readouterr().out
        assert '9.9.9.9:1' in out and '1 failed' in out

    def test_stats_json(self, tmp_path, monkeypatch, capsys):
        import json
        epg = tmp_path / 'e.xml'
        epg.write_text('<tv><channel id="X"/></tv>')
        monkeypatch.setattr(
            sys, 'argv', ['epg', '--stats', str(epg), '--json'])
        main()
        assert json.loads(capsys.readouterr().out)['channels']['total'] == 1

    def test_normal_mode_builds_app(self, monkeypatch):
        cls = MagicMock()
        cls._parse_args.return_value = SimpleNamespace(
            stats=None, proxy_stats=None, config='c.xml')
        monkeypatch.setattr('py_epg.main.PyEPG', cls)
        monkeypatch.setattr(sys, 'argv', ['epg', '-c', 'c.xml'])
        main()
        cls.return_value.run.assert_called_once()

    def test_python_m_entrypoint(self, tmp_path, monkeypatch, capsys):
        """python -m py_epg --stats <file> works end to end."""
        import runpy
        epg = tmp_path / 'e.xml'
        epg.write_text('<tv><channel id="X"/></tv>')
        monkeypatch.setattr(sys, 'argv', ['py_epg', '--stats', str(epg)])
        runpy.run_module('py_epg', run_name='__main__')
        assert 'CHANNELS' in capsys.readouterr().out
