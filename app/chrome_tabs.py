"""chrome_tabs.py — Qt-free core for Chrome's tab/session persistence formats.

Three genuinely different real on-disk formats, all reverse-engineered and
verified directly against this project's own real Android 14 JoshHickman
data (2026-09-05), none copied from any third-party tool for the outer
container logic in this file itself — only the PER-ENTRY navigation-record
payload (shared by two of the three) is vendored code (see
`ccl_chromium_snss.py`/`ccl_chromium_pickle.py`, MIT/CCL Forensics, and
`chrome_page_state.py`, Apache-2.0/Hindsight — both vendored 2026-09-05,
see their own module docstrings for full provenance):

1. **Desktop-style SNSS** (`app_chrome/Default/Sessions/Session_*`/`Tabs_*`)
   — Chrome-for-Android uses the IDENTICAL container format desktop Chrome
   does for its own tab-restore/session-restore files: magic `b"SNSS"` +
   version, then a stream of [uint16 length][1-byte command id][payload]
   records. `parse_snss_file` is a thin wrapper around the vendored
   `ccl_chromium_snss.SnssFile` — see that module's own docstring; this
   project contributes no new format knowledge here, only the wiring.

2. **Android TabState** (`app_tabs/<tab_id>/tab<N>`, no underscore — the
   real, current Chrome-for-Android per-tab persistence file, confirmed
   against real Chromium source: `chrome/browser/tabpersistence/android/
   java/.../TabStateFileManager.java`'s `readState()` for the OUTER
   wrapper, `chrome/browser/tab/web_contents_state.cc`'s
   `ExtractNavigationEntries`/`WriteSerializedNavigationsAsPickle` for the
   EMBEDDED native WebContentsState blob, and `components/sessions/core/
   serialized_navigation_entry.cc`'s `WriteToPickle`/`ReadFromPickle` for
   that blob's own per-entry payload — fetched directly from
   chromium.googlesource.com while building this, not assumed from memory
   or a third-party summary). `parse_android_tab_state` decodes:
   - The outer wrapper via Java `DataOutputStream`/`DataInputStream`
     semantics — BIG-ENDIAN, NOT base::Pickle's little-endian convention
     (confirmed empirically: the leading fields only decode to sane small
     integers as big-endian; as little-endian they're implausibly huge).
     Every field after `timestampMillis`/`size`/the embedded blob itself
     is genuinely OPTIONAL in the real Java source (each guarded by its
     own `try { } catch (EOFException)` there) — this parser mirrors that
     exact tolerance rather than treating a short/older-version file as
     an error: a field simply stays `None` once the buffer runs out,
     matching a real file from an older Chrome build that never wrote it.
   - The embedded WebContentsState blob AS a base::Pickle (LITTLE-ENDIAN,
     the opposite convention from the outer wrapper — confirmed, not
     assumed, by successfully decoding a real file's embedded blob this
     way after the outer-wrapper-only attempt failed): a StateHeader
     (is_off_the_record/entry_count/current_entry_index, all int32) then
     `entry_count` length-prefixed sub-pickles, each one a real
     `SerializedNavigationEntry` decoded via the VENDORED
     `ccl_chromium_snss.NavigationEntry.from_pickle` — the exact same
     function `parse_snss_file` above already uses for the desktop-style
     container, confirmed (via the Chromium source fetched above) to be
     the same real wire format Chrome-for-Android's own native code
     writes into this embedded blob, not merely a plausible-looking reuse.
   Verified end-to-end against 6 independent real files on this project's
   own Android 14 JoshHickman archive (`app_tabs/0/tab5` — a real 2024-02-08
   ad-redirect navigation with a real decoded title "Dulcetty" and a real
   `Link; FromApi` transition — plus 5 more single-entry real files under
   `app_tabs/custom_tabs/`), every one decoding its OWN embedded blob
   cleanly and its outer wrapper's `rootId` self-referencing the tab's own
   id from the filename (a real, internally-consistent cross-check, not
   assumed correct just because it parsed without raising).

3. **Legacy Android tab-state** (`app_tabs/<tab_id>/tab_state<N>`, WITH
   the underscore — a genuinely DIFFERENT, simpler format found alongside
   the modern TabState files on this same real device, NOT grounded in
   any current OR historical Chromium source this project could find
   (`chrome/browser/tabpersistence/`'s own file-naming constant is
   literally `"tab"`, never `"tab_state"`, in every revision checked) —
   flagged here plainly as reverse-engineered PURELY from real data, with
   no corroborating primary source, unlike format 2 above. Likely an
   older/superseded persistence mechanism whose files simply never got
   cleaned up after a Chrome update changed format — real, live tab ids in
   this exact directory use format 2 today, while these appear alongside
   them under different (stale) tab ids. Empirically confirmed via exact
   byte-for-byte structural fit (zero leftover/unaccounted bytes) across
   6 independent real files, all sharing the identical constant header
   values `[5, entry_count, 0, -1, ...]`:
       [int32 BE] x2 (constant 5, purpose unconfirmed -- plausibly a
                        format-version marker, by analogy with
                        webContentsStateVersion in format 2, but this is
                        a guess, not confirmed against any source)
       [int32 BE] entry_count
       [int32 BE] constant -1 (purpose unconfirmed)
       repeated `entry_count` times:
           [int32 BE] a per-entry integer, observed monotonically
                        non-decreasing across entries in the one real
                        multi-entry file available (values 5,13,15,19,21,24
                        for 6 entries) -- plausibly a navigation sequence
                        number, unconfirmed
           [uint16 BE length][UTF-8 bytes] -- the entry's own URL (Java
                        `DataOutputStream.writeUTF`/`readUTF` convention:
                        modified-UTF-8, but decoded here as plain UTF-8 --
                        confirmed indistinguishable for every real ASCII
                        URL checked so far, per-project convention of
                        preferring escalation over a wrong guess:
                        `errors="replace"` surfaces a genuine decode
                        failure as visible replacement characters rather
                        than raising and losing the whole row)
   No title, timestamp, or transition-type is recoverable from this
   format at all -- confirmed absent, not merely undecoded: the byte
   budget for each entry (one int32 + one length-prefixed URL, nothing
   else) leaves no room for it, unlike format 2's much richer embedded
   NavigationEntry. `parse_android_tab_state_legacy`'s own per-entry
   dict reflects this honestly (no `title`/`timestamp` keys at all,
   rather than a always-empty placeholder column).
"""

import io
import struct
import typing

import ccl_chromium_pickle
import ccl_chromium_snss
import chrome_page_state


def _epoch_seconds_from_datetime(dt) -> typing.Optional[float]:
    """A NavigationEntry.timestamp is already a converted, tz-naive
    datetime (ccl_chromium_pickle.EasyPickleIterator.read_datetime() does
    the 1601-epoch conversion internally) -- this project's own
    timestamp_fields convention needs a raw numeric value instead, so this
    re-derives plain Unix epoch seconds from it rather than carrying the
    datetime object into a Report row directly."""
    if dt is None:
        return None
    import datetime
    try:
        return (dt - datetime.datetime(1970, 1, 1)).total_seconds()
    except (TypeError, OverflowError, ValueError):
        return None


def _page_state_referrer(page_state_raw: bytes) -> typing.Optional[str]:
    """Best-effort referrer from the entry's own embedded PageState blob
    (chrome_page_state.parse_page_state) -- never raises, since a
    malformed/unsupported-version PageState is expected sometimes (see
    that module's own parse_page_state docstring) and must never cost the
    rest of this entry's already-decoded fields."""
    if not page_state_raw:
        return None
    try:
        state = chrome_page_state.parse_page_state(page_state_raw)
    except Exception:
        return None
    if state and state.top_frame:
        return state.top_frame.referrer
    return None


def _navigation_entry_to_dict(nav: "ccl_chromium_snss.NavigationEntry") -> dict:
    """Shared row-shaping for a real, successfully-decoded NavigationEntry
    -- used by both parse_snss_file (desktop-style SNSS container) and
    parse_android_tab_state (format 2's embedded WebContentsState blob),
    since both wrap the identical underlying record type."""
    referrer = nav.referrer_url or _page_state_referrer(nav.page_state_raw)
    return {
        "nav_index": nav.index,
        "url": nav.url,
        "title": nav.title,
        "transition": str(nav.transition_type),
        "referrer": referrer,
        "has_post_data": nav.has_post_data,
        "is_overriding_user_agent": nav.is_overriding_user_agent,
        "http_status": nav.http_status,
        "timestamp_epoch_s": _epoch_seconds_from_datetime(nav.timestamp),
    }


def parse_snss_file(data: bytes, is_session_file: bool) -> list:
    """Decode one real app_chrome/Default/Sessions/Session_*|Tabs_* file
    via the vendored ccl_chromium_snss.SnssFile. Returns one dict per
    real CommandUpdateTabNavigation record found -- every OTHER real
    command type in the file (window/tab structural bookkeeping: closed/
    selected/pinned/grouped/etc.) is real signal too but carries no
    url/title of its own to report as a Report-table row, so it's counted
    (see the returned list's own length) but not emitted as a row; no
    parser in this project currently declares a use for those structural
    commands. Never raises -- a single malformed/truncated record inside
    an otherwise-good file is skipped rather than losing every other
    record already read from it (SnssError/EasyPickleException/struct
    errors are all real, expected outcomes for a genuinely truncated or
    unsupported-version file, not a bug in this code).

    Each returned row also carries `raw_offset`/`raw_length` -- the exact
    file-absolute span of its own [uint16 length][1-byte command id]
    [payload] record, for the Hex panel's Record-mode jump (see
    `artifacts/android/chrome_sessions.py`'s own `record_source`
    declaration). Computed from consecutive commands' own `.offset`
    (already exposed by the vendored SnssFile, no reimplementation of its
    internal framing needed) rather than assumed from the payload lengths
    alone -- every command, processed or not, shares the identical outer
    framing, so the NEXT command's offset (or end-of-file for the last
    one) always correctly delimits where THIS one's record ends,
    including its own [length][id] header bytes."""
    file_type = (ccl_chromium_snss.SnssFileType.Session if is_session_file
                else ccl_chromium_snss.SnssFileType.Tab)
    try:
        snss = ccl_chromium_snss.SnssFile(file_type, io.BytesIO(data))
    except Exception:
        return []

    out = []
    try:
        commands = list(snss.iter_session_commands())
    except Exception:
        return out
    for i, cmd in enumerate(commands):
        if not isinstance(cmd, ccl_chromium_snss.NavigationEntry):
            continue
        try:
            row = _navigation_entry_to_dict(cmd)
        except Exception:
            continue
        row["session_id"] = cmd.session_id
        row["raw_offset"] = cmd.offset
        next_offset = commands[i + 1].offset if i + 1 < len(commands) else len(data)
        row["raw_length"] = next_offset - cmd.offset
        out.append(row)
    return out


def parse_android_tab_state(data: bytes) -> typing.Optional[dict]:
    """Decode one real app_tabs/<tab_id>/tab<N> file (Chrome-for-Android's
    current native TabState format -- see this module's own docstring,
    section 2). Returns a dict with the outer wrapper's own fields plus a
    "navigations" list (one dict per _navigation_entry_to_dict result,
    the tab's real navigation history), or None if even the mandatory
    leading fields (timestampMillis/size/the embedded blob itself) can't
    be read -- those three are never optional in the real Java source,
    unlike everything after them."""
    try:
        if len(data) < 12:
            return None
        timestamp_millis, = struct.unpack(">q", data[0:8])
        size, = struct.unpack(">i", data[8:12])
        if size < 0 or 12 + size > len(data):
            return None
        blob = data[12:12 + size]
        navigations = []
        is_off_the_record = entry_count = current_entry_index = None
        try:
            with ccl_chromium_pickle.EasyPickleIterator(blob) as outer:
                is_off_the_record = outer.read_bool()
                entry_count = outer.read_int32()
                current_entry_index = outer.read_int32()
                for _ in range(entry_count):
                    sub_len = outer.read_uint32()
                    # Absolute file offset of THIS entry's own sub-pickle
                    # bytes (right after its own 4-byte length prefix,
                    # which was just consumed) -- 12 accounts for the
                    # outer TabState wrapper's own fixed
                    # timestampMillis(8)+size(4) header before the blob
                    # begins. Captured here, not reconstructed after the
                    # fact, since read_aligned() below also seeks past
                    # any alignment padding, which is not part of the
                    # entry's own real content and must not be included
                    # in the Hex panel's highlighted span.
                    entry_raw_offset = 12 + outer._f.tell()
                    sub_bytes = outer.read_aligned(sub_len)
                    try:
                        with ccl_chromium_pickle.EasyPickleIterator(sub_bytes) as sub:
                            nav = ccl_chromium_snss.NavigationEntry.from_pickle(
                                sub, ccl_chromium_snss.TabRestoreIdType.CommandUpdateTabNavigation, 0)
                        nav_row = _navigation_entry_to_dict(nav)
                        nav_row["raw_offset"] = entry_raw_offset
                        nav_row["raw_length"] = sub_len
                        navigations.append(nav_row)
                    except Exception:
                        continue
        except Exception:
            pass  # blob present but undecodable -- still report the outer wrapper fields below

        f = io.BytesIO(data[12 + size:])

        def read_int():
            b = f.read(4)
            return struct.unpack(">i", b)[0] if len(b) == 4 else None

        def read_long():
            b = f.read(8)
            return struct.unpack(">q", b)[0] if len(b) == 8 else None

        def read_bool_field():
            b = f.read(1)
            return bool(b[0]) if len(b) == 1 else None

        def read_utf():
            b = f.read(2)
            if len(b) < 2:
                return None
            n = struct.unpack(">H", b)[0]
            s = f.read(n)
            return s.decode("utf-8", errors="replace") if len(s) == n else None

        parent_id = read_int()
        opener_app_id = read_utf() or None
        web_contents_state_version = read_int()
        read_long()          # obsolete sync id -- skipped, matches real readState()
        read_bool_field()    # obsolete shouldPreserve -- skipped
        theme_color = read_int()
        tab_launch_type_at_creation = read_int()
        root_id = read_int()
        user_agent = read_int()
        last_nav_committed_ts_millis = read_long()
        token_high = read_long()
        token_low = read_long()
        tab_has_sensitive_content = read_bool_field()
        is_pinned = read_bool_field()
        current_url = read_utf() or None

        tab_group_id = None
        if token_high is not None and token_low is not None and not (token_high == 0 and token_low == 0):
            tab_group_id = f"{token_high:016x}{token_low:016x}"

        return {
            # The tail region past the embedded WebContentsState blob --
            # parentId/rootId/themeColor/tabGroupId/etc. all live here,
            # not in the file's own head -- for the Hex panel's "Tab
            # Metadata" record_source entry (see chrome_app_tabs.py).
            "metadata_raw_offset": 12 + size,
            "metadata_raw_length": len(data) - (12 + size),
            "timestamp_millis": timestamp_millis,
            "parent_id": parent_id,
            "root_id": root_id,
            "opener_app_id": opener_app_id,
            "web_contents_state_version": web_contents_state_version,
            "theme_color": theme_color,
            "tab_launch_type_at_creation": tab_launch_type_at_creation,
            "user_agent": user_agent,
            "last_navigation_committed_timestamp_millis": last_nav_committed_ts_millis,
            "tab_group_id": tab_group_id,
            "tab_has_sensitive_content": tab_has_sensitive_content,
            "is_pinned": is_pinned,
            "current_url": current_url,
            "is_off_the_record": is_off_the_record,
            "entry_count": entry_count,
            "current_entry_index": current_entry_index,
            "navigations": navigations,
        }
    except Exception:
        return None


def parse_android_tab_state_legacy(data: bytes) -> typing.Optional[dict]:
    """Decode one real app_tabs/<tab_id>/tab_state<N> file (the simpler,
    unattributed legacy format -- see this module's own docstring,
    section 3). Returns a dict with the constant header fields plus a
    "navigations" list (one dict per entry: nav_index/entry_seq/url only
    -- no title/timestamp/transition, genuinely absent from this format,
    not merely unparsed), or None if even the fixed 20-byte header can't
    be read."""
    try:
        if len(data) < 20:
            return None
        header = struct.unpack(">5i", data[0:20])
        header_a, entry_count, header_c, header_d, _unused = header
        if entry_count < 0 or entry_count > 100000:
            return None
        pos = 20
        navigations = []
        for i in range(entry_count):
            entry_start = pos
            if pos + 4 > len(data):
                break
            entry_seq, = struct.unpack(">i", data[pos:pos + 4])
            pos += 4
            if pos + 2 > len(data):
                break
            length, = struct.unpack(">H", data[pos:pos + 2])
            pos += 2
            if pos + length > len(data):
                break
            url = data[pos:pos + length].decode("utf-8", errors="replace")
            pos += length
            navigations.append({
                "nav_index": i, "entry_seq": entry_seq, "url": url,
                "raw_offset": entry_start, "raw_length": pos - entry_start,
            })
        return {
            # The fixed 20-byte header itself (see this module's own
            # docstring, section 3) -- for the Hex panel's "Legacy Header"
            # record_source entry (see chrome_app_tabs.py).
            "header_raw_offset": 0,
            "header_raw_length": 20,
            "header_field_0": header_a,
            "entry_count": entry_count,
            "header_field_2": header_c,
            "header_field_3": header_d,
            "navigations": navigations,
            "trailing_bytes": len(data) - pos,
        }
    except Exception:
        return None
