"""chat_flags.py — "Red flags": rank the chats of a messaging report so the
examiner knows where to look first. Qt-free.

Keyword hits alone drown an examiner in false positives. Grooming (and
dealing) is a PATTERN: in Black et al. (2015) every offender used several
strategies (mean 4.6) — asking where the child lives, flattery, the
parents' schedule, sexual topics. Conversation-level scoring is also what
Microsoft Artemis and Roblox Sentinel do. So keyword hits (keyword_lists +
term_matcher) are evidence towards a score PER CHAT, built from:

  breadth   distinct categories hit in the same session (a session ends at
            a gap of more than SESSION_GAP_S); a category counts once per
            session however many hits it has, weighted by its strongest term
  rarity    a term hitting in many of the phone's chats says little: its
            weight falls with the share of chats it hits (only with at least
            MIN_CHATS_FOR_RARITY chats; fewer gives no basis, and that is said)
  request→media  a picture request from one person followed within
            REQUEST_MEDIA_S by media from the other: +2
  media-heavy    at least 10 media messages making up at least half the chat: +1
  deletions      deleted messages no disappearing-message timer explains: +1

The score orders chats; it proves nothing. Every point of it is explained
(reasons) and traced to message numbers (evidence)."""


SESSION_GAP_S = 2 * 3600
REQUEST_MEDIA_S = 30 * 60
MIN_CHATS_FOR_RARITY = 10
_PICTURE_CATEGORY = "Pictures / camera"
_MEANING = "Similar to known grooming messages"
OPENER_MIN_CHATS = 3
OPENER_MIN_CHARS = 8


def _category(term: dict) -> str:
    """Scoring category of a matched term."""
    cat = term.get("category") or ""
    if term.get("list") == "meaning":
        return cat
    if term.get("list") == "drugs":
        if cat.startswith("Drug name"):
            return "Drugs"
        if cat.startswith("Emoji"):
            return "Drug emoji"
        return "Drug trade"
    return cat or "Other"


def _sessions(messages: list) -> list:
    """[[message index, ...]] split at gaps longer than SESSION_GAP_S."""
    out, cur, prev = [], [], None
    for i, m in enumerate(messages):
        ts = m.get("ts")
        if cur and ts is not None and prev is not None and ts - prev > SESSION_GAP_S:
            out.append(cur)
            cur = []
        cur.append(i)
        if ts is not None:
            prev = ts
    if cur:
        out.append(cur)
    return out


def level(score: float) -> str:
    if score >= 5:
        return "●●●"
    if score >= 3:
        return "●●○"
    if score > 0:
        return "●○○"
    return ""


def score_chats(chats: dict, matcher, unexplained_missing: dict | None = None,
                meaning: dict | None = None) -> dict:
    """*chats*: {key: {'name', 'messages': [{speaker, text, media, ts, time}]}}
    (ai_summary.load_chat_messages). Returns {key: {score, level, categories,
    reasons, evidence: [(n, category, matched, how, speaker)], key_messages,
    window, rarity_note}}. *meaning*: meaning_check.chat_meaning() output;
    a message whose meaning resembles a known grooming message counts as the
    category "Similar to known grooming messages" in its session."""
    unexplained_missing = unexplained_missing or {}
    meaning = meaning or {}
    bank = meaning.get("_bank") or []
    import meaning_check
    hits_by_chat = {}
    chats_with_term: dict = {}
    for key, chat in chats.items():
        per_msg = []
        seen_terms = set()
        for i, m in enumerate(chat["messages"]):
            text = m.get("text") or ""
            if not text or (text.startswith("[") and text.endswith("]")):
                per_msg.append([])
                continue
            hs = matcher.find(text)
            per_msg.append(hs)
            for h in hs:
                seen_terms.add(id(h["term"]))
        mk = meaning.get(key)
        if mk:
            for i, (sc, j) in enumerate(zip(mk["scores"], mk["nearest"])):
                if sc > 0 and j is not None and i < len(per_msg):
                    ex = bank[j]
                    per_msg[i] = per_msg[i] + [{
                        "term": {"term": ex["text"], "category": meaning_check.CATEGORY,
                                 "list": "meaning", "source": ex.get("source", "")},
                        "matched": (chat["messages"][i].get("text") or "")[:80],
                        "how": f"meaning score {sc:.2f}, like \"{ex['text']}\" ({ex.get('source', '')})",
                        "start": 0, "end": 0}]
        hits_by_chat[key] = per_msg
        for t in seen_terms:
            chats_with_term[t] = chats_with_term.get(t, 0) + 1
    n_chats = len(chats)
    use_rarity = n_chats >= MIN_CHATS_FOR_RARITY
    # The owner's opening messages, compared across chats: the same opener
    # sent to many contacts is how a groomer works through targets. (The
    # same opener FROM many contacts is usually spam, so only "Me" counts.)
    import term_matcher
    openers: dict = {}
    first_by_chat: dict = {}
    for key, chat in chats.items():
        n = 0
        for i, m in enumerate(chat["messages"]):
            if m.get("speaker") != "Me":
                continue
            t = (m.get("text") or "").strip()
            if not t or (t.startswith("[") and t.endswith("]")):
                continue
            norm = " ".join(term_matcher._canon_phrase(t))
            if len(norm) >= OPENER_MIN_CHARS:
                openers.setdefault(norm, set()).add(key)
                first_by_chat.setdefault(key, []).append((norm, i + 1, t))
            n += 1
            if n >= 3:
                break

    def weight(term) -> float:
        if not use_rarity:
            return 1.0
        # full weight up to a fifth of the chats, then falling to 0.1 for a
        # term found in every chat
        share = chats_with_term.get(id(term), 0) / n_chats
        return 1.0 if share <= 0.2 else max(0.1, (1.0 - share) / 0.8)

    results = {}
    for key, chat in chats.items():
        msgs = chat["messages"]
        per_msg = hits_by_chat[key]
        best = (0.0, [], None)
        for sess in _sessions(msgs):
            cats: dict = {}
            for i in sess:
                for h in per_msg[i]:
                    c = _category(h["term"])
                    cats[c] = max(cats.get(c, 0.0), weight(h["term"]))
            s = sum(cats.values())
            if s > best[0]:
                best = (s, sess, cats)
        score, sess, cats = best
        reasons, evidence, key_msgs = [], [], []
        if cats:
            names = sorted(cats, key=lambda c: -cats[c])
            reasons.append(f"{len(cats)} categor{'y' if len(cats) == 1 else 'ies'} in one session: "
                           + ", ".join(names))
        # evidence: every hit in the best session (weakest-weight terms last)
        for i in sess or []:
            for h in per_msg[i]:
                evidence.append((i + 1, _category(h["term"]), h["matched"], h["how"],
                                 msgs[i].get("speaker", ""), weight(h["term"])))
        # request -> media from the other person
        pairs = []
        for i, hs in enumerate(per_msg):
            if not any(_category(h["term"]) == _PICTURE_CATEGORY for h in hs):
                continue
            asker, t0 = msgs[i].get("speaker"), msgs[i].get("ts")
            for j in range(i + 1, len(msgs)):
                tj = msgs[j].get("ts")
                if t0 is not None and tj is not None and tj - t0 > REQUEST_MEDIA_S:
                    break
                if msgs[j].get("media") and msgs[j].get("speaker") != asker:
                    pairs.append((i + 1, j + 1))
                    break
        if pairs:
            score += 2
            reasons.append("picture request then media from the other person: "
                           + ", ".join(f"#{a} → #{b}" for a, b in pairs[:5]))
            for a, b in pairs:
                key_msgs += [a, b]
        shared = [(norm, num, t) for norm, num, t in first_by_chat.get(key, [])
                  if len(openers.get(norm, ())) >= OPENER_MIN_CHATS]
        if shared:
            norm, num, t = max(shared, key=lambda x: len(openers[x[0]]))
            score += 1.5
            reasons.append(f"same opening message from Me in {len(openers[norm])} chats: "
                           f"#{num} \"{t[:60]}\"")
            key_msgs.append(num)
        n_media = sum(1 for m in msgs if m.get("media"))
        if n_media >= 10 and n_media >= len(msgs) / 2:
            score += 1
            reasons.append(f"mostly media: {n_media} of {len(msgs)} messages")
        missing = unexplained_missing.get(key) or 0
        if missing:
            score += 1
            reasons.append(f"{missing} deleted message(s) not explained by a timer")
        evidence.sort(key=lambda e: (-e[5], e[0]))
        for n, *_ in evidence:
            if n not in key_msgs:
                key_msgs.append(n)
        window = ""
        if sess:
            a, b = msgs[sess[0]].get("time", ""), msgs[sess[-1]].get("time", "")
            window = a[:16] if a[:16] == b[:16] else f"{a[:16]} – {b[11:16] if a[:10] == b[:10] else b[:16]}"
        results[key] = {
            "score": round(score, 2), "level": level(score),
            "categories": sorted(cats or {}, key=lambda c: -(cats or {})[c]),
            "reasons": reasons, "evidence": [e[:5] for e in evidence],
            "key_messages": key_msgs[:6], "window": window if cats else "",
            "session": (sess[0] + 1, sess[-1] + 1) if (sess and cats) else None,
            "rarity_note": ("" if use_rarity else
                            f"only {n_chats} chat(s): too few to tell which terms are common on this phone"),
        }
    return results


def flags_label(r: dict) -> str:
    """Short text for the Chat Groups 'Red Flags' column."""
    if not r or not r["score"]:
        return ""
    parts = []
    if r["categories"]:
        parts.append(", ".join(r["categories"][:4]) + ("…" if len(r["categories"]) > 4 else ""))
    extra = [x.split(":")[0] for x in r["reasons"][1 if r["categories"] else 0:]]
    if extra:
        parts.append("; ".join(extra))
    return f"{r['level']} " + " · ".join(parts)


def report_text(name: str, messages: list, r: dict, context: int = 2) -> str:
    """The 'RED FLAGS' part of the Text tab for one chat: why it scored, and
    the key messages with *context* messages either side, matched words
    marked »like this«."""
    if not r or not r["score"]:
        return (f"RED FLAGS — {name}\nNone found with the current keyword lists "
                "(Tools ▸ Keyword Lists). This is not proof that nothing is there: the lists "
                "only catch the words they contain.")
    out = [f"RED FLAGS — {name}   {r['level']}  score {r['score']:g}",
           "A ranking aid, not a finding: it shows where to read first.", ""]
    out += [f"• {x}" for x in r["reasons"]]
    if r["window"]:
        out.append(f"• strongest session: {r['window']}")
    if r["rarity_note"]:
        out.append(f"• note: {r['rarity_note']}")
    marks: dict = {}
    for n, cat, matched, how, _sp in r["evidence"]:
        marks.setdefault(n, []).append((matched, cat, how))
    shown = set()
    key_set = set(r["key_messages"])
    out += ["", "KEY MESSAGES (with context)"]
    for n in sorted(r["key_messages"]):
        lo, hi = max(1, n - context), min(len(messages), n + context)
        if all(k in shown for k in range(lo, hi + 1)):
            continue
        out.append("")
        for k in range(lo, hi + 1):
            if k in shown:
                continue
            shown.add(k)
            m = messages[k - 1]
            text = m.get("text") or "[no text]"
            for matched, c, _h in marks.get(k, []):
                if c != _MEANING:
                    text = text.replace(matched, f"»{matched}«", 1)
            arrow = "▶" if k in key_set or k in marks else " "
            out.append(f"{arrow} #{k}  {m.get('time', '')[:16]}  {m.get('speaker', '')}: {text}")
            for matched, cat, how in marks.get(k, []):
                if cat == _MEANING:
                    out.append(f"        {cat}: {how}")
                else:
                    out.append(f"        {cat}: »{matched}«" + ("" if how == "exact" else f" ({how})"))
    return "\n".join(out)


# ── kept results and background scoring ────────────────────────────────────
# Scoring every message of a big phone takes seconds (2 s at 50,000
# messages, measured 2026-11-17), so the result is kept per report in
# casecache.db (red_flags) under a fingerprint of everything it depends on,
# and worked out in a worker process (pure-Python CPU work in a QThread
# starves the GUI of the GIL, as found for the carver, A-81).

FLAGS_VERSION = 1          # bump when scoring changes: kept results are then not reused


def report_stamp(case_dir: str, script_name: str) -> str:
    """The report's rows as stored: table definition, row count, last rowid
    (a re-run rewrites the table). Cheap."""
    import hashlib
    import os
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{os.path.join(case_dir, 'caseresults.db')}?mode=ro", uri=True, timeout=5)
        try:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                            (f"artifact_{script_name}",)).fetchone()
            n, last = c.execute(f'SELECT count(*), max(rowid) FROM "artifact_{script_name}"').fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return 'no-report'
    return f"{hashlib.sha1((sql[0] if sql else '').encode()).hexdigest()[:12]}|{n}|{last}"


def fingerprint(case_dir: str, script_name: str, model: str) -> str:
    """What the red flags of *script_name* depend on: the report's rows
    (count, last rowid, table definition), the keyword lists (files' size
    and time), the meaning-check vectors kept for *model* (count) and
    FLAGS_VERSION. Cheap: a few queries, no message read."""
    import os
    import sqlite3
    import keyword_lists
    parts = [str(FLAGS_VERSION), script_name, report_stamp(case_dir, script_name)]
    for path in (keyword_lists.user_path(), *[keyword_lists.baseline_path(n) for n in keyword_lists.LISTS]):
        try:
            st = os.stat(path)
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append('-')
    try:
        c = sqlite3.connect(f"file:{os.path.join(case_dir, 'casecache.db')}?mode=ro", uri=True, timeout=5)
        try:
            parts.append(str(c.execute("SELECT count(*) FROM text_embeddings WHERE model=?",
                                       (model,)).fetchone()[0]))
        finally:
            c.close()
    except sqlite3.Error:
        parts.append('0')
    parts.append(model or '')
    return "|".join(parts)


def cache_get(case_dir: str, script_name: str, fp: str) -> dict | None:
    import json
    import os
    import sqlite3
    import zlib
    try:
        c = sqlite3.connect(f"file:{os.path.join(case_dir, 'casecache.db')}?mode=ro", uri=True, timeout=5)
        try:
            row = c.execute("SELECT fingerprint, flags FROM red_flags WHERE script_name=?",
                            (script_name,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    if not row or row[0] != fp:
        return None
    try:
        # a list of [key, flags] pairs: a JSON object would turn a number key into text
        return {k: v for k, v in json.loads(zlib.decompress(row[1]).decode('utf-8'))}
    except (ValueError, TypeError, zlib.error):
        return None


def cache_put(case_dir: str, script_name: str, fp: str, flags: dict) -> None:
    import json
    import zlib
    from db_utils import _open_cache_db
    try:
        c = _open_cache_db(case_dir)
        try:
            c.execute("INSERT OR REPLACE INTO red_flags (script_name, fingerprint, flags) VALUES (?,?,?)",
                      (script_name, fp, zlib.compress(json.dumps([[k, v] for k, v in flags.items()],
                                                      separators=(',', ':')).encode('utf-8'), 6)))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass                       # a cache: never break the page


def compute_for_report(case_dir: str, script_name: str, platform: str, missing: dict,
                       model: str) -> dict:
    """In the worker process: {chat key: flags} for *script_name*, each with
    the chat's 'name' added (so a page can list it without loading every
    message) and the meaning-check state. *missing*: {chat key: deleted
    messages no timer explains} from the Chat Groups summary."""
    import ai_summary
    import keyword_lists
    import term_matcher
    from artifact_runner import list_artifacts
    from artifact_db import list_completed_artifacts
    import sqlite3
    import os
    mods = {sn: m for sn, m in list_artifacts(platform)}
    mod = mods.get(script_name)
    decl = getattr(mod, 'chat_groups', None) or {}
    if not decl:
        return {}
    msgs = ai_summary.load_chat_messages(case_dir, script_name, decl,
                                         getattr(mod, 'timestamp_fields', {}) or {})
    matcher = term_matcher.Matcher(keyword_lists.enabled_terms())
    meaning = {}
    try:
        import meaning_check
        everything = {}
        c = sqlite3.connect(f"file:{os.path.join(case_dir, 'caseresults.db')}?mode=ro", uri=True, timeout=5)
        try:
            done = list_completed_artifacts(c)
        finally:
            c.close()
        for sn in done:
            d = getattr(mods.get(sn), 'chat_groups', None) if mods.get(sn) else None
            if not d:
                continue
            got = msgs if sn == script_name else ai_summary.load_chat_messages(
                case_dir, sn, d, getattr(mods[sn], 'timestamp_fields', {}) or {})
            everything.update({f"{sn}\x1f{k}": v for k, v in got.items()})
        own = {f"{script_name}\x1f{k}": v for k, v in msgs.items()}
        meaning = meaning_check.chat_meaning(case_dir, own, model, everything)
        meaning = {(k.split("\x1f", 1)[1] if "\x1f" in k else k): v for k, v in meaning.items()}
    except Exception:
        meaning = {}
    flags = score_chats(msgs, matcher, missing or {}, meaning)
    for k, f in flags.items():
        f['name'] = msgs.get(k, {}).get('name') or ''
        f['meaning'] = ('complete' if meaning.get('_complete') else
                        'partial' if meaning else 'not run')
        f['meaning_pool'] = meaning.get('_harmless', 0)
    return flags


_POOL = None


def _pool():
    global _POOL
    if _POOL is None:
        import atexit
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        _POOL = ProcessPoolExecutor(1, mp_context=mp.get_context('spawn'))
        atexit.register(lambda: _POOL.shutdown(wait=False, cancel_futures=True))
    return _POOL


def compute_in_process(case_dir: str, script_name: str, platform: str, missing: dict,
                       model: str) -> dict:
    """compute_for_report() in the worker process; blocks the calling
    (non-GUI) thread without holding the GIL."""
    return _pool().submit(compute_for_report, case_dir, script_name, platform,
                          missing, model).result()
