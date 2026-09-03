"""ai_summary.py — the AI Summary feature: gathers an artifact report's
ALREADY-COMPUTED rows from the current case's caseresults.db, filters to
the examiner-selected columns, and asks a local LLM (LM Studio by
default) to summarize them.

Deliberately reads from caseresults.db's artifact_<script_name> table —
the same source query_artifact (mcp_server.py) reads — rather than
re-running the parser, so this only ever summarizes what the examiner has
already reviewed as a normal Report, never a fresh, unreviewed parse.

Qt-free. Both the GUI dialog (artifact_viewer.py) and the MCP tools
(mcp_server.py) call this same code, so there is exactly one
implementation of "what gets sent and how" — the two surfaces can never
silently drift into sending different data for what looks like the same
report.

Nothing here writes to evidence or to caseresults.db. The only writes are
to the separate, non-evidentiary ai_summary_store.py settings file, and
those only happen when a caller explicitly asks to change a setting.

Map-reduce over natural time gaps, not fixed row counts (added after
direct testing against chrome_web_history's real 61 rows on a local
gpt-oss-20b model in LM Studio):
  - Sending all 61 rows unfiltered: hard failure, context length exceeded
    (32,575 chars — confirmed via the exact error LM Studio returns).
  - Same rows, columns narrowed + cell-length truncated: fit (16,482
    chars), but the model INVENTED a row/timestamp not present anywhere
    in the real data — unacceptable for casework.
  - Same again with an explicit anti-hallucination instruction: no
    fabrication, but the response was truncated partway (44 of 61 rows)
    — the model's own OUTPUT budget, not the input, became the limit.
  A single row cap (the old chunk_size-as-truncation behavior) silently
  drops the tail of the data either way. Splitting into several smaller
  LLM calls (map), each easily within context both ways, then combining
  their outputs into one final pass (reduce) is the standard fix — see
  run_summary's own docstring for why the SPLIT POINTS are chosen by
  code (real time gaps in the data) rather than left to the model to
  guess from a big blob of rows: this case's real activity clusters into
  6 sessions separated by gaps of 20 minutes to several weeks (confirmed
  by reading the actual timestamps), so gap-based splitting lands on
  exactly the boundaries a human reviewing the case would draw by hand,
  and never cuts a tightly-clustered redirect/sign-in chain in half the
  way a fixed "every N rows" cut risked doing.

  Tested a second time against a genuinely different real case (Android
  15 CTF25 Cellebrite, chrome_web_history, 75 rows spanning July-September
  2025) specifically to check the approach generalizes rather than being
  tuned to the first dataset's shape. That case's activity is far more
  fragmented (24 time-gap chunks vs. the first case's 6) and surfaced a
  real gap the first test never exercised: concatenating all 24 mini-
  summaries for the final reduce call came to 37,505 chars and failed
  with the SAME HTTP 400 context-length error the original single-shot
  per-row approach hit — the reduce step becomes the new bottleneck once
  there are enough map chunks, even though every individual map call fit
  fine. Fixed with a hierarchical (tree) reduce (_reduce_hierarchically)
  that batches mini-summaries under a safe character budget and recurses
  until one narrative remains, rather than always flattening every chunk
  into one reduce call. Re-verified end-to-end after the fix: all 75 rows
  accounted for across the 24 chunks, and the final narrative's specific
  claims (session timing, article titles, search terms) were spot-checked
  byte-for-byte against the real underlying rows with no fabrication.
"""

import datetime
import os
import re
import sqlite3
import time

import ai_summary_store
import local_llm

# Printable ASCII + common whitespace, same allowance header_scan.is_text
# uses elsewhere in this project for "is this actually text" checks.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _to_epoch_seconds(value, unit_code):
    """Raw timestamp value + this project's unit_code convention (see
    WRITING_ARTIFACT_PARSERS.md) -> Unix epoch seconds (float), or None
    if *value* doesn't look like a real number. Shared by
    _format_timestamp (display) and the time-gap chunker (grouping), so
    the two can never disagree about what a given raw value means."""
    try:
        secs = float(value)
    except (TypeError, ValueError):
        return None
    if unit_code == "ms":
        secs /= 1000
    elif unit_code == "cocoa_s":
        secs += 978307200
    elif unit_code == "cocoa_ns":
        secs = secs / 1e9 + 978307200
    elif unit_code == "webkit_us":
        secs = secs / 1e6 - 11644473600
    return secs


def _format_timestamp(value, unit_code):
    """One raw timestamp value -> human-readable UTC string. Always UTC
    (no handset/acquisition mode) — this goes into an LLM prompt for a
    narrative summary, not the examiner's own detailed per-case display,
    and UTC is this project's own documented default for exactly that
    reason. Returns the value unchanged if it doesn't convert cleanly —
    a summary with one unconverted timestamp is still useful; one that
    crashed isn't."""
    secs = _to_epoch_seconds(value, unit_code)
    if secs is None:
        return value
    try:
        return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return value


def _get_timestamp_fields(platform: str, script_name: str) -> dict:
    """script_name's timestamp_fields declaration, or {} if the module
    can't be found or declares none. Never raises — a lookup failure
    here should degrade to "send timestamps unconverted / no gap-based
    chunking", not break the whole summary."""
    try:
        import artifact_runner
        for name, mod in artifact_runner.list_artifacts(platform):
            if name == script_name:
                return dict(getattr(mod, "timestamp_fields", {}) or {})
    except Exception:
        pass
    return {}


def get_report_columns(case_dir: str, script_name: str) -> list:
    """All column names for script_name's artifact_<script_name> table in
    this case, or [] if that parser hasn't been run on this case yet (or
    the case has no caseresults.db at all)."""
    table = f"artifact_{script_name}"
    path = os.path.join(case_dir, "caseresults.db")
    if not os.path.isfile(path):
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


def format_rows(columns: list, rows: list, max_cell_len: int = 150) -> str:
    """Compact, token-cheap plain text: one header line, then one
    pipe-delimited line per row. Deliberately not JSON (repeats every
    column NAME on every row) and not a Python repr (adds quoting noise)
    — for a local model with a small context budget, the difference
    between this and JSON is real: JSON for the same rows measured
    roughly 2-3x more characters in testing against chrome_web_history.

    *max_cell_len* truncates any single value longer than this, with a
    '…[N more chars]' marker — found necessary by direct testing, not
    theoretical: dropping 15 unneeded columns from chrome_web_history
    barely changed the prompt size (32,575 -> 34,645 chars) because
    Google's own search-result URLs carry hundreds of characters of
    tracking parameters (gs_lcrp=...) that a column-selection checkbox
    can't address — the column itself (`url`) is genuinely wanted, only
    one specific value's LENGTH is the problem.

    Control characters (NUL bytes etc.) are stripped from every value —
    found necessary on real data, not theoretical: a recoverable_tables-
    carved row whose carving read past the end of real data into
    unallocated zero-filled space came back with a value padded with
    hundreds of literal NUL bytes. json.dumps would escape those rather
    than error, but they are pure noise for an LLM prompt either way."""
    def cell(v):
        if v is None:
            return ""
        s = _CONTROL_CHARS_RE.sub("", str(v))
        if len(s) > max_cell_len:
            return s[:max_cell_len] + f"…[{len(s) - max_cell_len} more chars]"
        return s

    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(cell(v) for v in row))
    return "\n".join(lines)


def _chunk_by_time_gap(rows: list, ts_index: int | None, ts_unit: str | None,
                       max_gap_minutes: float, max_rows_per_chunk: int) -> list:
    """Group *rows* (already in ascending time order if ts_index is set)
    into chunks along natural boundaries: a new chunk starts when either
    (a) the gap since the previous row's timestamp exceeds
    max_gap_minutes, or (b) the current chunk has reached
    max_rows_per_chunk rows — whichever comes first. (b) alone is the
    old fixed-size behavior; (a) is what actually preserves a
    multi-step flow's coherence — see the module docstring for the real
    numbers this was tuned against.

    A row with no usable timestamp (ts_index is None, or this specific
    row's value doesn't convert — e.g. a carved/recovered row missing
    its own timestamp column entirely) never triggers a gap split on its
    own account and is simply appended to whatever chunk is currently
    open, since there's no time axis to place it on."""
    if ts_index is None:
        return [rows[i:i + max_rows_per_chunk] for i in range(0, len(rows), max_rows_per_chunk)]

    chunks, current, prev_secs = [], [], None
    max_gap_secs = max_gap_minutes * 60
    for row in rows:
        secs = _to_epoch_seconds(row[ts_index], ts_unit)
        gap_too_big = (
            prev_secs is not None and secs is not None
            and abs(secs - prev_secs) > max_gap_secs
        )
        if current and (gap_too_big or len(current) >= max_rows_per_chunk):
            chunks.append(current)
            current = []
        current.append(row)
        if secs is not None:
            prev_secs = secs
    if current:
        chunks.append(current)
    return chunks


_REDUCE_PROMPT = (
    "You are assisting a digital forensics examiner. Below are several "
    "mini-summaries, each already describing one time-bounded session of "
    "activity, in chronological order. Combine them into ONE flowing "
    "narrative — the kind of write-up an examiner would put in a report: "
    "what subjects/topics the activity covered and roughly when, not a "
    "re-listing of each mini-summary one by one.\n"
    "Rules:\n"
    "1. Do not invent, approximate, or merge any timestamp, URL, name, or "
    "detail that is not already stated in the mini-summaries below.\n"
    "2. Preserve chronological order.\n"
    "3. Only describe two sessions as sharing a topic when their own "
    "stated subjects are genuinely and specifically the same thing (e.g. "
    "two entries about the exact same news story, or repeated visits to "
    "the exact same site). Do NOT invent a broad category label (like "
    "\"Sports\", \"Technology\", \"Government\") to group different "
    "real-world subjects together — that requires judgment the mini-"
    "summaries don't give you grounds for, and is a real, observed "
    "source of mischaracterization (e.g. a murder-investigation article "
    "and a Supreme Court story once got mislabeled \"sports\" this way "
    "just for appearing near real sports content). When sessions are "
    "only related by falling on the same day, describe them as separate, "
    "unrelated items rather than folding them into one theme.\n"
    "4. You may note a RECURRING subject across multiple sessions (e.g. "
    "several visits to the exact same site or story) as an observation — "
    "but only when the mini-summaries actually show that SPECIFIC "
    "subject repeating, never a broader category inferred across "
    "different subjects, and never a guess about overall interests from "
    "a single instance.\n"
    "5. Describe what was viewed/searched/done and its subject matter "
    "using only the words the mini-summaries themselves use — never "
    "infer a category, team, sport, or broader subject beyond what they "
    "explicitly state. Never guess at why something happened, mood, or "
    "intent.\n"
    "6. Avoid repeating \"the user\" in every sentence — vary the "
    "phrasing the way a written report would.\n\n{data}"
)

# Found necessary by direct testing on a real 75-row/24-chunk dataset (see
# run_summary's own docstring): concatenating all 24 mini-summaries came to
# 37,505 chars and the reduce call failed with the SAME HTTP 400 context-
# length error the original single-shot per-row approach hit at 32,575
# chars (see the module docstring) -- a dataset that fragments into enough
# small map chunks can make the REDUCE step the new bottleneck even though
# every individual map call fit fine. 15,000 is a conservative ceiling
# below the 16,482-char point already confirmed to fit, leaving headroom
# for the reduce prompt's own instruction text and the model's output.
_REDUCE_BATCH_MAX_CHARS = 15000


def _emit(progress, message: str) -> None:
    """Call *progress* (a callable taking one short string) if it's set --
    lets a caller (the GUI dialog's log, or anything else) see a
    lightweight, call-by-call sense of what's happening (sending a chunk,
    a response coming back, how many calls are left) without needing to
    know anything about chunking/reduce internals itself. Never lets a
    broken progress callback abort a real summary run."""
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        pass


def _render_chunk_block(c: dict) -> str:
    label = f"[{c['rows']} rows"
    if c.get("time_range"):
        label += f", {c['time_range']}"
    label += "]"
    return f"{label}\n{c['text']}"


def _combine_time_range(chunk_results: list):
    """First chunk's start to last chunk's end, across a batch being
    combined into one intermediate summary -- keeps a multi-level reduce's
    intermediate results labeled with a real range, not just the
    innermost original chunk's own range."""
    ranges = [c["time_range"] for c in chunk_results if c.get("time_range")]
    if not ranges:
        return None
    first = ranges[0].split(" to ")[0]
    last = ranges[-1].split(" to ")[-1]
    return first if first == last else f"{first} to {last}"


def _reduce_once(chunk_results: list, conn_settings: dict, prompt_template: str):
    """One reduce LLM call combining *chunk_results* (2+ entries) into a
    single {rows, time_range, text} summary. Returns (result, error,
    prompt) -- error is None on success."""
    data_text = "\n\n".join(_render_chunk_block(c) for c in chunk_results)
    prompt = prompt_template.replace("{data}", data_text)
    result = local_llm.call_chat(
        conn_settings["endpoint"], conn_settings["api_key"], conn_settings["model"], prompt)
    if "error" in result:
        return None, result["error"], prompt
    combined = {
        "rows": sum(c["rows"] for c in chunk_results),
        "time_range": _combine_time_range(chunk_results),
        "text": result["text"],
    }
    return combined, None, prompt


def _reduce_hierarchically(chunk_results: list, conn_settings: dict, prompt_template: str,
                           max_batch_chars: int = _REDUCE_BATCH_MAX_CHARS, progress=None):
    """Combine 2+ mini-summaries into ONE final narrative, recursing in
    size-bounded batches rather than always doing it in a single call --
    see _REDUCE_BATCH_MAX_CHARS's own comment for why a single flat reduce
    call isn't safe once there are enough chunks. Each pass packs
    consecutive summaries (already in chronological order, so a batch
    boundary never reorders anything) into batches under max_batch_chars,
    reduces each multi-item batch, and leaves any lone leftover batch
    untouched -- then repeats on the resulting (shorter) list until only
    one summary remains. Returns (final_text, error, last_prompt_used);
    error is None on success, and last_prompt_used is the most recent
    reduce prompt actually sent (for the caller's own transparency/debug
    field), or None if nothing was ever combined (shouldn't happen since
    this is only called with 2+ chunks)."""
    level = list(chunk_results)
    last_prompt = None
    round_num = 0
    while len(level) > 1:
        round_num += 1
        batches, current, current_len = [], [], 0
        for c in level:
            block_len = len(_render_chunk_block(c))
            if current and current_len + block_len > max_batch_chars:
                batches.append(current)
                current, current_len = [], 0
            current.append(c)
            current_len += block_len
        if current:
            batches.append(current)
        # Every summary alone already exceeds the budget -- packing can't
        # help, so fall back to pairing consecutive summaries instead of
        # looping forever on an unchanged batch count.
        if len(batches) == len(level):
            batches = [level[i:i + 2] for i in range(0, len(level), 2)]

        multi = [b for b in batches if len(b) > 1]
        if multi:
            _emit(progress, f"Combine round {round_num}: merging {len(level)} "
                            f"summaries into {len(batches)} batch(es)…")

        next_level = []
        call_num = 0
        round_durations = []
        for batch in batches:
            if len(batch) == 1:
                next_level.append(batch[0])
                continue
            call_num += 1
            _emit(progress, f"  Sending combine call {call_num}/{len(multi)} "
                            f"(round {round_num}, {len(batch)} summaries)…")
            t0 = time.time()
            combined, error, prompt = _reduce_once(batch, conn_settings, prompt_template)
            dt = time.time() - t0
            if error:
                _emit(progress, f"  Combine call {call_num}/{len(multi)} "
                                f"(round {round_num}) failed after {dt:.1f}s: {error}")
                return None, error, last_prompt
            round_durations.append(dt)
            avg = sum(round_durations) / len(round_durations)
            remaining = len(multi) - call_num
            eta = f", ~{avg * remaining:.0f}s left in this round" if remaining else ""
            _emit(progress, f"  Combine call {call_num}/{len(multi)} "
                            f"(round {round_num}) returned in {dt:.1f}s{eta}.")
            last_prompt = prompt
            next_level.append(combined)
        level = next_level
    return level[0]["text"], None, last_prompt


def save_summary(case_dir: str, script_name: str, result: dict) -> None:
    """Persist a SUCCESSFUL run_summary() result (no 'error' key) so it can
    be shown again later without re-running the LLM -- the Artifact tree's
    app-group root view (see e.g. artifacts/android/chrome_web_history.py's
    group_overview_mode) reads whatever was last generated here, the same
    way a normal Report table persists in caseresults.db rather than
    needing to be regenerated on every view. Overwrites any previous
    summary for the same script_name -- same "always rebuilt fresh"
    convention as artifact_db.write_artifact_results. Both the GUI
    (AISummaryDialog) and the MCP run_ai_summary tool call this after a
    successful run, so either surface generating a summary makes it
    visible to the other."""
    path = os.path.join(case_dir, "caseresults.db")
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ai_summaries ("
            "script_name TEXT PRIMARY KEY, text TEXT, total_rows INTEGER, "
            "chunk_count INTEGER, generated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO ai_summaries (script_name, text, total_rows, chunk_count, generated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(script_name) DO UPDATE SET "
            "text=excluded.text, total_rows=excluded.total_rows, "
            "chunk_count=excluded.chunk_count, generated_at=excluded.generated_at",
            (script_name, result["text"], result["total_rows"], result["chunk_count"],
             # Same 'YYYY-MM-DDTHH:MM:SS' (no microseconds/offset) format
             # db_utils.py's run_log uses for run_at/completed_at -- keeps
             # this compatible with artifact_viewer.py's existing
             # _local_date tool-provenance formatter instead of inventing
             # a second timestamp convention.
             datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


def load_summary(case_dir: str, script_name: str):
    """The last-persisted summary for *script_name* in this case:
    {text, total_rows, chunk_count, generated_at}, or None if one has
    never been generated (or caseresults.db/the table doesn't exist yet)
    -- a normal, expected state for a report nobody has run AI Summary on
    yet, not an error."""
    path = os.path.join(case_dir, "caseresults.db")
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_summaries'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT text, total_rows, chunk_count, generated_at FROM ai_summaries "
            "WHERE script_name = ?", (script_name,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    text, total_rows, chunk_count, generated_at = row
    return {"text": text, "total_rows": total_rows, "chunk_count": chunk_count,
            "generated_at": generated_at}


def get_settings(case_dir: str, script_name: str) -> dict:
    """{columns, chunk_size, max_gap_minutes, prompt, all_columns} for
    one report — the full picture a settings UI (or an MCP caller)
    needs: the examiner's current selection AND the full set it was
    chosen from."""
    all_columns = get_report_columns(case_dir, script_name)
    settings = ai_summary_store.get_report_settings(script_name, all_columns)
    settings["all_columns"] = all_columns
    return settings


def run_summary(case_dir: str, script_name: str, platform: str = "android",
                max_total_rows: int = 500, progress=None) -> dict:
    """Run the CURRENTLY CONFIGURED summary for one report against ALL of
    this case's real data for it (not just the first chunk_size rows —
    see the module docstring for why a single-call row cap silently
    dropped data). Splits into time-gap-bounded chunks, gets one
    mini-summary per chunk, then (if there was more than one chunk)
    combines them into a final narrative in one more call.

    Returns on success:
      {text, total_rows, chunk_count, chunks: [{rows, time_range, text}],
       columns_sent, prompt_used}
    ('text' is the final combined narrative; 'chunks' is included for
    transparency/debugging — each chunk's own mini-summary and how many
    real rows it covered, so a caller can verify nothing was silently
    dropped.) Returns {error: ...} if the report hasn't been run yet, no
    model is configured, or a local LLM call failed (see
    local_llm.call_chat — never raises for that).

    *max_total_rows* is a hard safety cap on how much data this will
    ever try to process in one go, independent of chunking — protects
    against a pathologically large report turning into dozens of LLM
    calls with no examiner-visible warning.

    *progress*, if given, is called with one short string each time a
    chunk is sent/returned or a combine call happens — enough for a
    caller (the GUI's AISummaryDialog log) to show "what's being done"
    without needing the actual chunk text. Never required, never lets a
    broken callback abort the run (see _emit)."""
    all_columns = get_report_columns(case_dir, script_name)
    if not all_columns:
        return {"error": f"{script_name!r} has not been run on this case yet "
                          "— open its Report in the Artifact Viewer first"}

    settings = ai_summary_store.get_report_settings(script_name, all_columns)
    columns = settings["columns"]
    max_rows_per_chunk = settings["chunk_size"]
    max_gap_minutes = settings.get("max_gap_minutes", ai_summary_store.DEFAULT_MAX_GAP_MINUTES)
    prompt_template = settings["prompt"]
    if "{data}" not in prompt_template:
        return {"error": "prompt template has no {data} placeholder — nothing would be sent"}

    ts_fields = _get_timestamp_fields(platform, script_name)
    # ALL selected timestamp columns need display conversion (found by
    # direct testing: chrome_overview declares two — first_activity AND
    # last_activity — and an earlier version of this function only
    # converted whichever one happened to come first in column order,
    # silently leaving the other as a raw webkit-microsecond integer).
    ts_indexes = {i: ts_fields[c] for i, c in enumerate(columns) if c in ts_fields}
    # Exactly ONE column drives time-gap chunking/labeling — the first
    # timestamp column found, same choice as before; a report with more
    # than one timestamp field (chrome_overview) still only has one
    # sensible axis to group ROWS along.
    ts_field_name = next((c for c in columns if c in ts_fields), None)
    ts_index = columns.index(ts_field_name) if ts_field_name else None
    ts_unit = ts_fields.get(ts_field_name)

    table = f"artifact_{script_name}"
    path = os.path.join(case_dir, "caseresults.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        col_list = ", ".join(f'"{c}"' for c in columns)
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        order_by = f' ORDER BY "{ts_field_name}"' if ts_field_name else ''
        rows = conn.execute(
            f'SELECT {col_list} FROM "{table}"{order_by} LIMIT ?', (max_total_rows,)
        ).fetchall()
    finally:
        conn.close()

    conn_settings = ai_summary_store.get_connection()
    if not conn_settings["model"]:
        return {"error": "no model configured — call local_llm.list_models "
                         "against the connection endpoint and set one first"}

    row_chunks = _chunk_by_time_gap(rows, ts_index, ts_unit, max_gap_minutes, max_rows_per_chunk)

    def render(row_group):
        if not ts_indexes:
            return format_rows(columns, row_group)
        converted = []
        for row in row_group:
            row = list(row)
            for i, unit in ts_indexes.items():
                row[i] = _format_timestamp(row[i], unit)
            converted.append(tuple(row))
        return format_rows(columns, converted)

    # Single chunk: no reduce pass needed, one call does it all.
    if len(row_chunks) == 1:
        data_text = render(row_chunks[0])
        if total > len(rows):
            data_text += f"\n\n[showing first {len(rows)} of {total} total rows]"
        prompt = prompt_template.replace("{data}", data_text)
        _emit(progress, f"Sending {len(row_chunks[0])} row(s) to model (1 call)…")
        t0 = time.time()
        result = local_llm.call_chat(
            conn_settings["endpoint"], conn_settings["api_key"], conn_settings["model"], prompt)
        dt = time.time() - t0
        if "error" in result:
            _emit(progress, f"Call failed after {dt:.1f}s: {result['error']}")
            return result
        _emit(progress, f"Response received in {dt:.1f}s. Done.")
        return {
            "text": result["text"], "total_rows": total, "chunk_count": 1,
            "chunks": [{"rows": len(row_chunks[0]), "text": result["text"]}],
            "columns_sent": columns, "prompt_used": prompt,
        }

    # Map: one mini-summary per time-bounded chunk.
    chunk_results = []
    chunk_durations = []
    for i, group in enumerate(row_chunks):
        data_text = render(group)
        prompt = prompt_template.replace("{data}", data_text)
        _emit(progress, f"Sending chunk {i + 1}/{len(row_chunks)} to model "
                        f"({len(group)} rows)…")
        t0 = time.time()
        result = local_llm.call_chat(
            conn_settings["endpoint"], conn_settings["api_key"], conn_settings["model"], prompt)
        dt = time.time() - t0
        if "error" in result:
            _emit(progress, f"Chunk {i + 1}/{len(row_chunks)} failed after {dt:.1f}s: {result['error']}")
            return {"error": f"chunk failed ({len(group)} rows): {result['error']}",
                    "completed_chunks": chunk_results}
        # Running average of chunks done SO FAR drives the ETA for what's
        # left -- a real estimate from this run's own pace, not a guess,
        # though it assumes remaining chunks take roughly as long as the
        # ones seen so far (reasonable since chunk sizes are capped by the
        # same chunk_size setting throughout one run).
        chunk_durations.append(dt)
        avg = sum(chunk_durations) / len(chunk_durations)
        remaining = len(row_chunks) - (i + 1)
        eta = f", ~{avg * remaining:.0f}s left ({remaining} chunk(s))" if remaining else ""
        _emit(progress, f"Chunk {i + 1}/{len(row_chunks)} returned in {dt:.1f}s{eta}.")
        time_range = None
        if ts_index is not None:
            first_ts = _format_timestamp(group[0][ts_index], ts_unit)
            last_ts = _format_timestamp(group[-1][ts_index], ts_unit)
            time_range = f"{first_ts} to {last_ts}" if first_ts != last_ts else first_ts
        chunk_results.append({"rows": len(group), "time_range": time_range, "text": result["text"]})

    # Reduce: combine the mini-summaries into one final narrative. Done in
    # size-bounded batches (see _reduce_hierarchically) rather than one
    # flat call over every chunk -- confirmed necessary by direct testing:
    # a 24-chunk real dataset's concatenated mini-summaries (37,505 chars)
    # hit the same context-length failure the map step was built to avoid.
    _emit(progress, f"All {len(row_chunks)} chunks done — combining into a final narrative…")
    final_text, error, reduce_prompt = _reduce_hierarchically(
        chunk_results, conn_settings, _REDUCE_PROMPT, progress=progress)
    if error:
        _emit(progress, f"Combine step failed: {error}")
        return {"error": f"reduce step failed: {error}",
                "completed_chunks": chunk_results}
    _emit(progress, "Done.")

    return {
        "text": final_text, "total_rows": total, "chunk_count": len(row_chunks),
        "chunks": chunk_results, "columns_sent": columns, "prompt_used": reduce_prompt,
    }
