"""Category → App → Report arrangement (artifact_categories.py) for Run
Artifact Parsers and the Artifact Viewer."""
import json
import os
from types import SimpleNamespace

import pytest

import artifact_categories as ac

ROOT = os.path.join(os.path.dirname(__file__), '..')


@pytest.mark.parametrize('platform', ['ios', 'android'])
def test_every_shipped_parser_has_a_category(platform):
    # every report the loader makes, the deleted-records reports declared in
    # a main report's deleted_tables included (A-95, 2026-11-27)
    import artifact_runner
    mods, errors = artifact_runner.load_artifacts(platform)
    assert not errors, errors
    scripts = sorted(sn for sn, _m in mods)
    by_name = dict(mods)
    missing = [s for s in scripts if ac.classify(platform, s, by_name[s])[0] == ac.OTHER]
    assert not missing, missing
    with open(ac.store_path(), encoding='utf-8') as f:
        named = set(json.load(f)[platform])
    assert not (named - set(scripts)), named - set(scripts)     # no stale names


def test_arrange_groups_apps_with_several_reports_only():
    m = lambda name, **kw: SimpleNamespace(name=name, **kw)
    items = [('whatsapp', m('WhatsApp Messages')), ('whatsapp_poll_votes', m('WhatsApp Poll Votes')),
             ('viber', m('Viber')), ('chrome_cookies', m('Chrome Cookies')),
             ('device_info', m('Device Info')), ('mystery', m('Mystery'))]
    got = ac.arrange('android', items)
    cats = [c for c, _ in got]
    assert cats == ['Chats & Messaging', 'Browsers', 'Device & App Info', ac.OTHER]
    chats = dict(got)['Chats & Messaging']
    assert chats[0][0] == 'WhatsApp' and [s for s, _ in chats[0][1]] == ['whatsapp', 'whatsapp_poll_votes']
    assert chats[1] == (None, [items[2]])                    # Viber: one report, no app node
    assert dict(got)['Browsers'] == [(None, [items[3]])]     # one Chrome report run: no app node
    assert dict(got)[ac.OTHER][0][1][0][0] == 'mystery'      # shown, never hidden


def test_a_parsers_own_declaration_wins():
    mod = SimpleNamespace(name='X', category='Browsers', app_group_label='Mine')
    assert ac.classify('android', 'whatsapp', mod) == ('Browsers', 'Mine')
    bad = SimpleNamespace(name='X', category='Not a category')
    assert ac.classify('android', 'whatsapp', bad)[0] == ac.OTHER


def test_attachment_links_table_is_not_a_report():
    import sqlite3
    from artifact_db import list_completed_artifacts
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE artifact_whatsapp (x)')
    c.execute('CREATE TABLE artifact_attachment_links (x)')
    assert list_completed_artifacts(c) == ['whatsapp']
