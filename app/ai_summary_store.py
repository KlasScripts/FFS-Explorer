"""ai_summary_store.py — global (cross-case) settings for the AI Summary
feature: the local LLM connection (endpoint/api_key/model — LM Studio by
default) plus, per artifact report, which columns get sent, how many rows
per chunk, and the editable prompt template. Same dev/frozen-path
persistence convention as research_store.py / parser_versions.json — a
single hand-editable JSON file, not per-case.

Report settings are keyed by bare script_name (e.g. "chrome_search"),
matching query_artifact's own `name` parameter in mcp_server.py — no
platform prefix, since MCP callers already address reports that way and
a second naming convention here would only invite mismatches.
"""

import json
import os
import sys

DEFAULT_PROMPT = (
    "You are assisting a digital forensics examiner. Below is a table of "
    "real activity rows, all from roughly the same short session. Write a "
    "short narrative (2-4 sentences of flowing prose, NOT a table or "
    "bullet list) describing what this session shows. Follow these rules "
    "exactly:\n"
    "1. Every timestamp, name, and value you mention MUST be copied "
    "exactly from a row below. Never invent, approximate, round, or merge "
    "a timestamp or value that is not literally present in a row. This "
    "applies even when two rows have the SAME or very similar title but "
    "different timestamps and URLs (e.g. the same search repeated, or a "
    "site visited more than once) -- they are still separate events; "
    "never attribute a later row's specific URL/action to an earlier "
    "row's timestamp just because the titles look alike.\n"
    "2. You may quote or closely paraphrase a title/URL/message's own "
    "words to describe its subject. Do NOT infer anything not explicitly "
    "named in that text -- e.g. do not guess which team, sport, category, "
    "or broader subject something belongs to unless that specific word "
    "already appears in the title/URL/text itself. If a title's real "
    "subject requires outside knowledge to interpret (sports team "
    "rosters, brand names, etc.), just quote the title rather than "
    "characterizing it further -- guessing at a category is a common "
    "source of mischaracterization even when every literal fact stays "
    "accurate.\n"
    "3. Do not guess at WHY something happened, mood, or motive -- only "
    "describe what the data shows.\n"
    "4. Avoid repeating \"the user\" in every sentence -- vary the "
    "phrasing (\"a search for X led to...\", \"this was followed by...\") "
    "the way a written forensic report would.\n"
    "5. If you are not certain a detail is in the table, omit it rather "
    "than guess.\n"
    "6. Do not add any row, timestamp, or value that does not appear "
    "verbatim below.\n\n{data}"
)
# Chosen and verified against real data, not a guess: this case's actual
# Chrome activity (Josh Hickman's documented Android 14 image) clusters
# into 6 real-world sessions separated by gaps of 20 minutes to several
# weeks -- a 30-minute default lands cleanly between "a user paused mid-
# session" and "this is a genuinely separate session", confirmed by
# reading the real timestamp gaps directly rather than assumed.
DEFAULT_MAX_GAP_MINUTES = 30
# Rows per chunk ceiling, independent of the time-gap split above -- a
# safety net for a single real-world session so tightly packed it never
# hits a gap large enough to split on its own.
DEFAULT_CHUNK_SIZE = 50
DEFAULT_ENDPOINT = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODELS_ENDPOINT = "http://localhost:1234/v1"

_cache = {"stat": None, "data": None}


def store_path() -> str:
    """Location of ai_summary_settings.json (next to exe when frozen, else
    config/) — same convention as research_store.store_path()."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "ai_summary_settings.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "ai_summary_settings.json")


def load() -> dict:
    """Return the whole settings dict; cached by file mtime+size, same
    pattern as research_store.load()."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["stat"], _cache["data"] = None, {}
        return {}
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    _cache["stat"], _cache["data"] = stat, data
    return data


def save(data: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        st = os.stat(path)
        _cache["stat"], _cache["data"] = (st.st_mtime_ns, st.st_size), data
    except OSError:
        _cache["stat"] = None


def get_connection() -> dict:
    """{endpoint, api_key, model} — defaults to LM Studio's own standard
    local address when never configured. `model` defaults to '' (empty),
    since which model is actually loaded varies and local_llm.list_models
    is how a caller discovers the real id rather than guessing one."""
    conn = load().get("connection", {})
    return {
        "endpoint": conn.get("endpoint") or DEFAULT_ENDPOINT,
        "api_key": conn.get("api_key") or "",
        "model": conn.get("model") or "",
    }


def set_connection(endpoint: str = None, api_key: str = None, model: str = None) -> None:
    data = load()
    conn = dict(data.get("connection", {}))
    if endpoint is not None:
        conn["endpoint"] = endpoint
    if api_key is not None:
        conn["api_key"] = api_key
    if model is not None:
        conn["model"] = model
    data["connection"] = conn
    save(data)


def get_report_settings(script_name: str, all_columns: list) -> dict:
    """{columns, chunk_size, prompt} for one report. First call for a
    report defaults `columns` to ALL of *all_columns* (nothing excluded
    until the examiner deliberately trims it) — *all_columns* is passed
    in rather than looked up here so this module stays free of any
    caseresults.db/sqlite dependency; ai_summary.py owns that lookup."""
    reports = load().get("reports", {})
    saved = reports.get(script_name, {})
    columns = saved.get("columns")
    if not columns:
        columns = list(all_columns)
    else:
        # Drop any saved column name that no longer exists on this report
        # (a parser's own columns can change between versions) — silently
        # keeping a stale name would make column filtering look like it
        # includes something that was actually dropped.
        columns = [c for c in columns if c in all_columns]
        if not columns:
            columns = list(all_columns)
    return {
        "columns": columns,
        "chunk_size": int(saved.get("chunk_size") or DEFAULT_CHUNK_SIZE),
        "max_gap_minutes": float(saved.get("max_gap_minutes") or DEFAULT_MAX_GAP_MINUTES),
        "prompt": saved.get("prompt") or DEFAULT_PROMPT,
    }


def set_report_settings(script_name: str, columns: list = None,
                        chunk_size: int = None, max_gap_minutes: float = None,
                        prompt: str = None) -> None:
    """Update only the fields given — omitting one leaves it unchanged."""
    data = load()
    reports = dict(data.get("reports", {}))
    saved = dict(reports.get(script_name, {}))
    if columns is not None:
        saved["columns"] = list(columns)
    if chunk_size is not None:
        saved["chunk_size"] = int(chunk_size)
    if max_gap_minutes is not None:
        saved["max_gap_minutes"] = float(max_gap_minutes)
    if prompt is not None:
        saved["prompt"] = prompt
    reports[script_name] = saved
    data["reports"] = reports
    save(data)
