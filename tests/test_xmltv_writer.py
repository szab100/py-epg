#!/usr/bin/env python3
"""Tests for the XMLTV serializer wrapper."""

import io

from xmltv.models import Channel, DisplayName, Programme, Title, Tv

from py_epg.common.xmltv_writer import write_file_from_xml


def test_output_has_xml_10_declaration(tmp_path):
    """Consumers (Plex, xmltv validators) expect a standard XML 1.0
    declaration; previously the file had none and targeted XML 1.1."""
    tv = Tv(
        channel=[Channel(id='CH1',
                         display_name=[DisplayName(content=['Chan'])])],
        programme=[Programme(
            channel='CH1', start='20240115060000 +0100',
            stop='20240115070000 +0100',
            title=[Title(content=['Cím'], lang='hu')])])
    out = tmp_path / 'epg.xml'
    write_file_from_xml(out, tv)
    content = out.read_text(encoding='utf-8')
    assert content.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert '<programme' in content
    assert 'Cím' in content
