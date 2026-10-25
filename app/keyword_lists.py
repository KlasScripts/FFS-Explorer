"""keyword_lists.py — the grooming and drug keyword lists. Qt-free.

Two layers (owner request 2026-10-25: "this is the base line then if a user
want to add to the list they can and it is mark as such"):

- The BASELINE ships with the app (config/keyword_list_<name>.json; in a
  frozen build under _internal/config, see config_seed.py for why) and is
  always read from there, so an upgrade brings new baseline terms. Every
  baseline term carries its source (a paper or DEA report) and evidence.
- The examiner's CHANGES live in one editable file next to the exe
  (keyword_lists_user.json): terms they added (origin "user", or "llm" for
  an approved model suggestion, with who/when/model), and on/off choices.
  A baseline term can be switched off but not deleted, so its provenance
  is never lost.

A term dict has: term, category, source, evidence, everyday,
everyday_reason, enabled, origin (+ added_at / added_by / model for added
terms). Its key is "<term lowercased>|<category>".
"""

import datetime
import json
import os
import sys

LISTS = ("grooming", "drugs")
LIST_LABELS = {"grooming": "Grooming / sexual exploitation", "drugs": "Drugs"}
_USER_FILE = "keyword_lists_user.json"
_cache: dict = {}


def _check(name: str) -> str:
    if name not in LISTS:
        raise ValueError(f"unknown keyword list {name!r}")
    return name


def baseline_path(name: str) -> str:
    fn = f"keyword_list_{_check(name)}.json"
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, "config", fn)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", fn)


def user_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), _USER_FILE)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", _USER_FILE)


def key(term: dict) -> str:
    return f"{term['term'].strip().lower()}|{term.get('category', '')}"


def _read(path: str) -> dict:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {}
    _cache[path] = (sig, doc)
    return doc


def _user_doc() -> dict:
    doc = _read(user_path())
    return doc if isinstance(doc, dict) else {}


def _write_user(doc: dict) -> None:
    path = user_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = dict(doc)
    doc["_note"] = ("The examiner's own changes to FFS Explorer's keyword lists: added terms "
                    "and on/off choices. The baseline lists ship with the app; see "
                    "keyword_lists.py.")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _cache.pop(path, None)


def sources(name: str) -> dict:
    """{source key: citation} for the baseline list."""
    return dict(_read(baseline_path(name)).get("sources") or {})


def load(name: str) -> list:
    """Every term of list *name*: baseline terms with the examiner's on/off
    choices applied, then the examiner's added terms."""
    base = _read(baseline_path(name)).get("terms") or []
    user = (_user_doc().get("lists") or {}).get(_check(name)) or {}
    enabled = user.get("enabled") or {}
    out = []
    for t in base:
        t = dict(t)
        t.setdefault("origin", "baseline")
        k = key(t)
        if k in enabled:
            t["enabled"] = bool(enabled[k])
        out.append(t)
    have = {key(t) for t in out}
    for t in user.get("added") or []:
        t = dict(t)
        k = key(t)
        if k in have:
            continue
        if k in enabled:
            t["enabled"] = bool(enabled[k])
        out.append(t)
        have.add(k)
    return out


def enabled_terms(names=LISTS) -> list:
    """Enabled terms of the given lists, each with its list name in 'list'."""
    out = []
    for n in names:
        for t in load(n):
            if t.get("enabled"):
                out.append({**t, "list": n})
    return out


def set_enabled(name: str, term_key: str, on: bool) -> None:
    doc = _user_doc()
    lists = dict(doc.get("lists") or {})
    lst = dict(lists.get(_check(name)) or {})
    en = dict(lst.get("enabled") or {})
    en[term_key] = bool(on)
    lst["enabled"] = en
    lists[name] = lst
    doc["lists"] = lists
    _write_user(doc)


def add_term(name: str, term: str, category: str, origin: str = "user", added_by: str = "",
             note: str = "", model: str = "") -> dict:
    """Add an examiner term (origin 'user') or an approved model suggestion
    (origin 'llm', with *model*). Returns the stored term."""
    term = (term or "").strip()
    if not term:
        raise ValueError("empty term")
    t = {"term": term, "category": category or "Added by user", "source": origin.upper(),
         "evidence": note or ("added by the examiner" if origin == "user"
                              else f"suggested by local model {model}, approved by the examiner"),
         "everyday": False, "everyday_reason": "", "enabled": True, "origin": origin,
         "added_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
         "added_by": added_by}
    if model:
        t["model"] = model
    doc = _user_doc()
    lists = dict(doc.get("lists") or {})
    lst = dict(lists.get(_check(name)) or {})
    added = [x for x in (lst.get("added") or []) if key(x) != key(t)]
    added.append(t)
    lst["added"] = added
    lists[name] = lst
    doc["lists"] = lists
    _write_user(doc)
    return t


def remove_added(name: str, term_key: str) -> bool:
    """Remove an examiner-added term. Baseline terms can only be switched
    off (set_enabled), never removed. Returns True if something was removed."""
    doc = _user_doc()
    lists = dict(doc.get("lists") or {})
    lst = dict(lists.get(_check(name)) or {})
    added = lst.get("added") or []
    kept = [x for x in added if key(x) != term_key]
    if len(kept) == len(added):
        return False
    lst["added"] = kept
    lists[name] = lst
    doc["lists"] = lists
    _write_user(doc)
    return True
