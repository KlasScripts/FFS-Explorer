"""report_extras.py — what opening a report needs beyond its rows, worked
out once when the report is made and kept in casecache.db (2026-11-21).

Opening the 500,000-message test report used to scan it on the window
thread several times: which columns have values (5.15 s), every distinct
media path for thumbnails (2.65 s), every row id (4 s inside the app), and,
for a messaging report, the Chat Groups summary (17 s at case open, from
the report tree). All of it now comes from here:

  rows            the row count (a report table is written fresh, rows
                  1..N, so the row ids are range(1, N + 1))
  filled:<scope>  columns with a value: the whole report, and each chat
                  (report_columns_store.filled_columns_scan)
  media           {media column: distinct non-empty values}
  chat_index      [(key, name, messages)] per chat, for the report tree
  chat_groups     the Chat Groups summary (build_chat_groups) without
                  bookmark counts, and each linked row's chat and paths,
                  so bookmarks are counted at display (apply_bookmarks)

Each entry is kept under the report's stamp (chat_flags.report_stamp), so a
re-run is never matched with an older run's extras. Rebuilt from
caseresults.db, so safe to lose (ASSUMPTIONS.md A-53). A report made before
this existed gets them once, on a background thread, when it is opened.
Qt-free."""

import array
import base64
import collections
import json
import os
import re
import sqlite3
import zlib

EXTRAS_VERSION = 2   # bump when what is saved, or how, changes: older extras are then rebuilt

_TABLE = '''CREATE TABLE IF NOT EXISTS report_extras (
    script_name TEXT NOT NULL,
    key         TEXT NOT NULL,
    stamp       TEXT NOT NULL,
    value       BLOB NOT NULL,
    PRIMARY KEY (script_name, key))'''


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def duration_seconds(label) -> int | None:
    """'Off' -> 0, '7 days' -> 604800, '24 hours' -> 86400, '90 seconds' -> 90."""
    text = str(label or '').strip()
    if text.lower() == 'off':
        return 0
    m = re.match(r'^(\d+)\s+(day|hour|second)s?$', text)
    if not m:
        return None
    return int(m.group(1)) * {'day': 86400, 'hour': 3600, 'second': 1}[m.group(2)]


# ── Store ─────────────────────────────────────────────────────────────────

def save(case_dir: str, script_name: str, stamp: str, entries: dict) -> None:
    """Replace every extra of *script_name* with *entries* {key: value}."""
    from db_utils import _open_cache_db
    c = _open_cache_db(case_dir)
    try:
        c.execute(_TABLE)
        c.execute('DELETE FROM report_extras WHERE script_name=?', (script_name,))
        c.executemany('INSERT INTO report_extras VALUES (?,?,?,?)', (
            (script_name, k, f'{EXTRAS_VERSION}|{stamp}',
             zlib.compress(json.dumps(v, separators=(',', ':')).encode('utf-8'), 6))
            for k, v in entries.items()))
        c.commit()
    finally:
        c.close()


def load(case_dir: str, script_name: str, stamp: str, key: str):
    """The kept value of *key* for this version of the report, or None."""
    from db_utils import _open_cache_db
    try:
        c = _open_cache_db(case_dir)
        try:
            c.execute(_TABLE)
            row = c.execute('SELECT stamp, value FROM report_extras WHERE script_name=? AND key=?',
                            (script_name, key)).fetchone()
        finally:
            c.close()
    except Exception:
        return None
    if row is None or row[0] != f'{EXTRAS_VERSION}|{stamp}':
        return None
    try:
        return json.loads(zlib.decompress(row[1]).decode('utf-8'))
    except Exception:
        return None


# ── Chat Groups ───────────────────────────────────────────────────────────

def build_chat_groups(conn, script_name: str, decl: dict, mod) -> tuple[list, list]:
    """(chats, link_rows) for a messaging report from its `chat_groups`
    declaration (see ArtifactViewerMixin._art_chat_groups). chats: one dict
    per conversation, sorted by name, 'bookmarked' 0. link_rows: [chat key,
    [paths]] for each row with a link/media path, for apply_bookmarks.
    ([], []) if the table can't be read. *conn*: caseresults.db."""
    from sqlite_carve import _epoch_seconds
    units = getattr(mod, 'timestamp_fields', {}) or {}

    def _cols(v):
        return [v] if isinstance(v, str) else list(v or ())
    key_col = decl['key']
    names = _cols(decl.get('name')) or [key_col]
    senders = _cols(decl.get('participants'))
    members_col = decl.get('members')
    dir_col = decl.get('direction', 'direction')
    sent = set(decl.get('sent', ('Sent', 'Outgoing')))
    system = set(decl.get('system', ()))
    media_cols = _cols(decl.get('media'))
    prefixes = tuple(decl.get('media_prefixes', ()))
    msg_col = decl.get('message', 'message')
    time_col = decl.get('time')
    unit = units.get(time_col) if time_col else None
    deleted = decl.get('deleted') or {}
    type_col = decl.get('type')
    icon_col = decl.get('icon')
    sort_col, issued_col = decl.get('sort'), decl.get('issued')
    setting_col, expires_col = decl.get('setting'), decl.get('expires')
    dfe_col = decl.get('deleted_for_everyone')
    call_re = re.compile(decl['call_pattern']) if decl.get('call_pattern') else None
    call_col = decl.get('call_field')
    url_re = re.compile(r'https?://', re.IGNORECASE)
    link_cols = _cols(getattr(mod, 'link_fields', None) or getattr(mod, 'media_fields', None))
    try:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({_q("artifact_" + script_name)})')}
        if key_col not in cols:
            return [], []
        want = [c for c in dict.fromkeys(
            [key_col, dir_col, msg_col, time_col, members_col, *names, *senders,
             *media_cols, *deleted, *link_cols, type_col, sort_col, issued_col,
             setting_col, expires_col, dfe_col, icon_col, decl.get('call_field')])
                if c and c in cols]
        rows = conn.execute('SELECT ' + ', '.join(_q(c) for c in want)
                            + f' FROM {_q("artifact_" + script_name)}').fetchall()
    except Exception:
        return [], []
    at = {c: i for i, c in enumerate(want)}
    link_at = [at[c] for c in link_cols if c in at]

    def _get(r, c):
        return r[at[c]] if c in at else None
    chats: dict = {}
    link_rows = []
    for r in rows:
        key = _get(r, key_col) or ''
        c = chats.setdefault(key, {
            'names': collections.Counter(), 'people': collections.Counter(),
            'messages': 0, 'incoming': 0, 'outgoing': 0, 'media_in': 0, 'media_out': 0,
            'deleted': 0, 'calls': 0 if call_re else None, 'links': 0, 'bookmarked': 0,
            'types': collections.Counter(), 'icons': collections.Counter(),
            'me': {}, 'notes': {},
            'seq': [], 'issued': None, 'settings': [], 'dfe_me': 0, 'dfe_them': 0,
            'expiry_seen': False,
            'times': [], 'first': None, 'last_in': None, 'last_out': None})
        c['messages'] += 1
        if _get(r, type_col):
            c['types'][_get(r, type_col)] += 1
        if _get(r, icon_col):
            c['icons'][_get(r, icon_col)] += 1
        text_all = str(_get(r, msg_col) or '')
        for col, vals in deleted.items():
            v = _get(r, col)
            if (vals == '*' and v not in (None, '', 'False', '0', 0, False)) or \
                    (vals != '*' and str(v) in vals):
                c['deleted'] += 1
                break
        call_text = str(_get(r, call_col) or '') if call_col else text_all
        if call_re and call_re.search(call_text):
            c['calls'] += 1
        if url_re.search(text_all):
            c['links'] += 1
        if link_at:
            paths = [p.strip() for i in link_at for p in str(r[i] or '').split('\n') if p.strip()]
            if paths:
                link_rows.append([key, paths])
        name = next((_get(r, n) for n in names if _get(r, n)), '')
        if name:
            c['names'][name] += 1
        if _get(r, dir_col) in system:
            continue
        out = _get(r, dir_col) in sent
        c['outgoing' if out else 'incoming'] += 1
        if not out:
            who = next((_get(r, n) for n in senders if _get(r, n)), '')
            if who and not str(who).startswith('['):
                c['people'][who] += 1
        for who in str(_get(r, members_col) or '').split(', ') if members_col else ():
            if who.startswith('Me'):
                c['me'][who] = None
            elif who and not who.startswith('['):
                c['people'][who] += 1
            elif who:
                c['notes'][who] = None
        text = str(_get(r, msg_col) or '')
        if (any(_get(r, m) for m in media_cols) or (prefixes and text.startswith(prefixes))):
            c['media_out' if out else 'media_in'] += 1
        ts = _epoch_seconds(_get(r, time_col), unit) if unit else None
        if sort_col:
            try:
                c['seq'].append((int(_get(r, sort_col)), ts))
            except (TypeError, ValueError):
                pass
            try:
                c['issued'] = max(c['issued'] or 0, int(_get(r, issued_col)))
            except (TypeError, ValueError):
                pass
        if setting_col and _get(r, setting_col):
            c['settings'].append((ts, out, duration_seconds(_get(r, setting_col))))
        if expires_col and _get(r, expires_col) not in (None, '', '0'):
            c['expiry_seen'] = True
        dfe = str(_get(r, dfe_col) or '') if dfe_col else ''
        if dfe:
            c['dfe_me' if '(by me)' in dfe else 'dfe_them'] += 1
        if ts:
            c['times'].append(ts)
            c['first'] = ts if c['first'] is None else min(c['first'], ts)
            k = 'last_out' if out else 'last_in'
            c[k] = ts if c[k] is None else max(c[k], ts)
    result = []
    for key, c in chats.items():
        result.append({
            'key': key,
            'name': c['names'].most_common(1)[0][0] if c['names'] else (key or '[no conversation]'),
            'participants': ', '.join(list(c['me']) + [p for p, _ in c['people'].most_common()]
                                      + list(c['notes'])),
            'people': len(c['people']),
            'type': c['types'].most_common(1)[0][0] if c['types'] else None,
            'icon': c['icons'].most_common(1)[0][0] if c['icons'] else '',
            **{k: c[k] for k in ('messages', 'incoming', 'outgoing', 'media_in',
                                 'media_out', 'deleted', 'calls', 'links', 'bookmarked',
                                 'times', 'first', 'last_in', 'last_out',
                                 'seq', 'issued', 'settings', 'dfe_me', 'dfe_them', 'expiry_seen')},
            'has_sort': bool(sort_col), 'has_setting': bool(setting_col)})
    result.sort(key=lambda d: str(d['name']).casefold())
    return result, link_rows


def _tuples(chats: list) -> list:
    """JSON gives lists back; the summary code reads seq/settings entries
    as tuples, as build_chat_groups makes them."""
    for c in chats:
        c['seq'] = [tuple(x) for x in c.get('seq') or ()]
        c['settings'] = [tuple(x) for x in c.get('settings') or ()]
    return chats


# Kept compactly: one json.loads of the plain form (13 MB for 500,000
# messages, most of it every message's time and every linked row's paths)
# held Python's lock 0.9 s, so it froze the window from any thread
# (2026-11-21). Times are packed doubles; the link rows two joined strings.
_SEP = '\x1f'


def _pack(chats: list, link_rows: list) -> dict:
    out = []
    for c in chats:
        c = dict(c)
        c['times'] = base64.b64encode(array.array('d', c['times']).tobytes()).decode('ascii')
        out.append(c)
    return {'chats': out,
            'link_keys': _SEP.join(str(k) for k, _p in link_rows),
            'link_paths': _SEP.join('\n'.join(p) for _k, p in link_rows)}


def _unpack(saved: dict) -> tuple[list, list]:
    chats = saved['chats']
    for c in chats:
        t = array.array('d')
        t.frombytes(base64.b64decode(c['times']))
        c['times'] = t.tolist()     # floats; one C call (a Python loop here took 1 s)
    keys = saved['link_keys'].split(_SEP) if saved['link_keys'] else []
    paths = saved['link_paths'].split(_SEP) if saved['link_paths'] else []
    return chats, [[k, p.split('\n')] for k, p in zip(keys, paths)]


def apply_bookmarks(chats: list, link_rows: list, bookmarked: set) -> list:
    """Each chat's 'bookmarked': its rows with a bookmarked path (a row
    counts once). The bookmarks change while the case is open, so they
    are counted here, never kept."""
    counts = collections.Counter()
    if bookmarked:
        for key, paths in link_rows:
            if any(p in bookmarked for p in paths):
                counts[key] += 1
    for c in chats:
        c['bookmarked'] = counts.get(c['key'], 0)
    return chats


# ── Build when the report is made ─────────────────────────────────────────

def build_all(case_dir: str, script_name: str, mod) -> dict:
    """Work out and save every extra of *script_name* (see the module
    docstring). Runs where the report is made (ArtifactRunnerWorker), or on
    a background thread for a report made before this existed. Returns the
    saved entries. Raises when the report can't be read."""
    import chat_flags
    import report_columns_store
    table = f'artifact_{script_name}'
    stamp = chat_flags.report_stamp(case_dir, script_name)
    decl = getattr(mod, 'chat_groups', None) or {}
    c = sqlite3.connect(f"file:{os.path.join(case_dir, 'caseresults.db')}?mode=ro", uri=True, timeout=5)
    try:
        columns = [r[1] for r in c.execute(f'PRAGMA table_info({_q(table)})')]
        n = c.execute(f'SELECT count(*) FROM {_q(table)}').fetchone()[0]
        group_col = decl.get('key') if decl.get('key') in columns else None
        entries = {'rows': n}
        for scope, filled in report_columns_store.filled_columns_scan(
                c, table, columns, group_col).items():
            entries['filled:' + scope] = sorted(filled)
        media = {}
        for col in getattr(mod, 'media_fields', ()) or ():
            if col in columns:
                media[col] = [v for (v,) in c.execute(
                    f'SELECT DISTINCT {_q(col)} FROM {_q(table)} '
                    f"WHERE {_q(col)} IS NOT NULL AND {_q(col)} != ''")]
        entries['media'] = media
        if group_col:
            chats, link_rows = build_chat_groups(c, script_name, decl, mod)
            entries['chat_index'] = [[ch['key'], ch['name'], ch['messages']] for ch in chats]
            entries['chat_groups'] = _pack(chats, link_rows)
    finally:
        c.close()
    save(case_dir, script_name, stamp, entries)
    return entries


def chat_groups(case_dir: str, script_name: str, stamp: str, bookmarked: set):
    """The kept Chat Groups summary with current bookmark counts, or None."""
    got = load(case_dir, script_name, stamp, 'chat_groups')
    if got is None:
        return None
    try:
        chats, link_rows = _unpack(got)
    except (KeyError, ValueError):
        return None                # an older layout: built again
    return apply_bookmarks(_tuples(chats), link_rows, bookmarked)
