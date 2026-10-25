"""meaning_check.py — does a message MEAN something like known grooming
messages, whatever words it uses? Qt-free.

Keyword lists miss paraphrase ("send one without your hoodie"). This scores
each message by meaning, the way Roblox Sentinel does (open source, Apache
2.0, github.com/Roblox/Sentinel — formula re-implemented here, credited):

  every message is turned into a meaning vector by a local embedding model
  (LM Studio; nomic-embed-text, shortened to DIM numbers), then compared with
    - a bank of HARMFUL examples (config/meaning_bank_grooming.json: quoted
      offender messages from the research, and marked example phrasings of
      the strategies it describes), and
    - a bank of HARMLESS examples: the phone's OWN messages (as Roblox uses
      its own platform's normal chat), which fits each owner's way of writing.
  score = log( mean(e^sim) over the TOP_K_HARMFUL most similar harmful examples
             / mean(e^sim) over the TOP_K_HARMLESS most similar messages
               from the phone's OTHER chats ),
  set to 0 when below MIN_SCORE (Sentinel's calculate_contrastive_score and
  min_score_to_consider; our k and threshold differ, see below). A chat's spread of scores is summarised by its
  skewness (Sentinel's default aggregate; needs MIN_FOR_SKEW messages):
  "mostly normal with a few strong outliers".

Pure Python (math.sumprod, 3.12+): numpy is excluded from the build.
Embeddings are cached in casecache.db (text_embeddings), so a message is
only embedded once per model. A score orders messages; it proves nothing.
"""

import hashlib
import json
import math
import os
import random
import sqlite3
import sys
from array import array

DIM = 256                 # nomic-embed-text v1.5 is trained to be cut to 256
PREFIX = "classification: "   # nomic task prefix
# Sentinel's defaults are top_k 5 and min score 0.1, set for a 13,000-example
# harmful bank and a model with a wider similarity spread. nomic squeezes
# similarities into ~0.65-0.95 and our bank is small, so (2026-10-25,
# measured on IOS17 + one synthetic chat — PROVISIONAL until measured on a
# labelled set): the single closest harmful example against the 3 closest
# messages from the phone's OTHER chats, threshold 0.05.
TOP_K_HARMFUL = 1
TOP_K_HARMLESS = 3
MIN_SCORE = 0.05
MIN_FOR_SKEW = 10
BENIGN_SAMPLE = 2000
MIN_CHARS = 6
MIN_HARMLESS = 200        # fewer of the phone's own messages than this: say scores are less reliable
CATEGORY = "Similar to known grooming messages"


def bank_path(name: str = "grooming") -> str:
    fn = f"meaning_bank_{name}.json"
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, "config", fn)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", fn)


def load_bank(name: str = "grooming") -> dict:
    try:
        with open(bank_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"examples": [], "sources": {}}


def eligible(text: str) -> bool:
    t = (text or "").strip()
    return len(t) >= MIN_CHARS and not (t.startswith("[") and t.endswith("]"))


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _norm(vec) -> array:
    v = list(vec[:DIM])
    n = math.sqrt(math.sumprod(v, v)) or 1.0
    return array("f", (x / n for x in v))


def _db(case_dir: str) -> sqlite3.Connection:
    from db_utils import _open_cache_db
    return _open_cache_db(case_dir)


def cached_vectors(case_dir: str, model: str, texts) -> dict:
    """{text: vector} for the texts already embedded with *model*."""
    out = {}
    want = {_hash(t): t for t in texts}
    if not want:
        return out
    conn = _db(case_dir)
    try:
        keys = list(want)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ",".join("?" * len(chunk))
            for h, blob in conn.execute(
                    f"SELECT text_hash, vec FROM text_embeddings WHERE model=? AND text_hash IN ({q})",
                    [model, *chunk]):
                v = array("f")
                v.frombytes(blob)
                out[want[h]] = v
    finally:
        conn.close()
    return out


def embed_texts(case_dir: str, texts, conn_settings: dict, model: str, progress=None,
                should_stop=None, batch: int = 64) -> dict:
    """Embed every text not cached yet; returns {'embedded': n, 'cached': m}
    or {'error': ...}. Saves each batch as it goes, so a stop keeps work."""
    import local_llm
    texts = list(dict.fromkeys(t for t in texts if t))
    have = cached_vectors(case_dir, model, texts)
    todo = [t for t in texts if t not in have]
    done = 0
    for i in range(0, len(todo), batch):
        if should_stop and should_stop():
            break
        chunk = todo[i:i + batch]
        r = local_llm.embed(conn_settings["endpoint"], conn_settings["api_key"], model,
                            [PREFIX + t for t in chunk])
        if "error" in r:
            return {"error": r["error"], "embedded": done, "cached": len(have)}
        conn = _db(case_dir)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO text_embeddings (model, text_hash, dim, vec) VALUES (?,?,?,?)",
                [(model, _hash(t), DIM, _norm(v).tobytes()) for t, v in zip(chunk, r["vectors"])])
            conn.commit()
        finally:
            conn.close()
        done += len(chunk)
        if progress and (i // batch) % 10 == 0:
            progress(f"  embedded {done:,} of {len(todo):,} new messages…")
    return {"embedded": done, "cached": len(have)}


def _top_mean_exp(vec, bank, k: int, skip=None) -> tuple:
    sims = []
    for j, b in enumerate(bank):
        if skip is not None and j in skip:
            continue
        sims.append((math.sumprod(vec, b), j))
    sims.sort(reverse=True)
    top = sims[:k]
    if not top:
        return 0.0, None
    return sum(math.exp(s) for s, _ in top) / len(top), top[0][1]


def score_vector(vec, harmful: list, harmless: list, skip=None) -> tuple:
    """(score, index of the nearest harmful example) — Sentinel's contrastive
    log-ratio, clipped at 0 and below MIN_SCORE. *skip*: indexes of
    *harmless* to leave out (the chat's own messages)."""
    pos, nearest = _top_mean_exp(vec, harmful, TOP_K_HARMFUL)
    neg, _ = _top_mean_exp(vec, harmless, TOP_K_HARMLESS, skip)
    if pos <= 0 or neg <= 0:
        return 0.0, nearest
    lr = math.log(pos / neg)
    return (lr if lr >= MIN_SCORE else 0.0), nearest


def skewness(xs: list):
    """Sample skewness (Fisher-Pearson g1); None for fewer than MIN_FOR_SKEW."""
    n = len(xs)
    if n < MIN_FOR_SKEW:
        return None
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 == 0:
        return 0.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    return m3 / m2 ** 1.5


def _benign_sample(chats: dict) -> list:
    """[(text, chat key)] — up to BENIGN_SAMPLE of the phone's own messages."""
    seen, pairs = set(), []
    for key, c in chats.items():
        for m in c["messages"]:
            t = m.get("text")
            if eligible(t) and t not in seen:
                seen.add(t)
                pairs.append((t, key))
    if len(pairs) > BENIGN_SAMPLE:
        pairs = random.Random(0).sample(pairs, BENIGN_SAMPLE)
    return pairs


def run(case_dir: str, chats: dict, conn_settings: dict, model: str, progress=None,
        should_stop=None) -> dict:
    """Embed the harmful bank and every eligible message of *chats*
    (ai_summary.load_chat_messages). Scoring itself is chat_meaning()."""
    bank = [e["text"] for e in load_bank().get("examples", [])]
    if not bank:
        return {"error": "the harmful example bank is missing"}
    msgs = [m["text"] for c in chats.values() for m in c["messages"] if eligible(m.get("text"))]
    if progress:
        progress(f"Meaning check: {len(set(msgs)):,} messages, {len(bank)} harmful examples, "
                 f"model {model}")
    r = embed_texts(case_dir, bank + msgs, conn_settings, model, progress, should_stop)
    return r


def chat_meaning(case_dir: str, chats: dict, model: str, harmless_chats: dict | None = None) -> dict:
    """{chat key: {'scores': [per message], 'nearest': [bank index or None],
    'skew': float or None}} using cached vectors only, plus '_bank' (the
    examples) and '_complete' (False when some messages aren't embedded yet,
    i.e. the check hasn't been run, or only partly), '_harmless' (how many of
    the phone's messages were compared with). *harmless_chats*: every chat
    on the phone to sample normal messages from (default *chats*); keys
    must not collide with *chats* except for the same chat. {} if nothing
    cached."""
    bank_doc = load_bank()
    examples = bank_doc.get("examples", [])
    bank_texts = [e["text"] for e in examples]
    texts = [m["text"] for c in chats.values() for m in c["messages"] if eligible(m.get("text"))]
    if not texts or not bank_texts:
        return {}
    vecs = cached_vectors(case_dir, model, set(texts) | set(bank_texts))
    harmful = [vecs[t] for t in bank_texts if t in vecs]
    if len(harmful) < len(bank_texts) // 2:
        return {}
    pool = harmless_chats or chats
    pool_texts = {m["text"] for c in pool.values() for m in c["messages"] if eligible(m.get("text"))}
    vecs.update(cached_vectors(case_dir, model, pool_texts - set(vecs)))
    sample = [(t, k) for t, k in _benign_sample(pool) if t in vecs]
    harmless = [vecs[t] for t, _ in sample]
    if len(harmless) < TOP_K_HARMLESS + 1:
        return {}
    # a message's own chat is left out of its harmless comparison: the
    # harmless side is the phone's OTHER chats (as Sentinel compares with
    # other users' normal chat)
    own: dict = {}
    for j, (_t, k) in enumerate(sample):
        own.setdefault(k, set()).add(j)
    texts_by_chat = {k: {m.get("text") for m in c["messages"]} for k, c in chats.items()}
    for k, c in pool.items():
        texts_by_chat.setdefault(k, {m.get("text") for m in c["messages"]})
    for j, (t, _k) in enumerate(sample):       # the same text in several chats: leave out everywhere it occurs
        for k2, ts in texts_by_chat.items():
            if t in ts:
                own.setdefault(k2, set()).add(j)
    harmful_examples = [e for e in examples if e["text"] in vecs]
    out = {"_bank": harmful_examples, "_sources": bank_doc.get("sources", {}),
           "_complete": all(t in vecs for t in texts), "_harmless": len(harmless)}
    for key, chat in chats.items():
        scores, nearest = [], []
        for m in chat["messages"]:
            v = vecs.get(m.get("text")) if eligible(m.get("text")) else None
            if v is None:
                scores.append(0.0)
                nearest.append(None)
                continue
            s, j = score_vector(v, harmful, harmless, own.get(key))
            scores.append(s)
            nearest.append(j if s > 0 else None)
        out[key] = {"scores": scores, "nearest": nearest,
                    "skew": skewness([s for s, m in zip(scores, chat["messages"])
                                      if eligible(m.get("text"))])}
    return out
