"""term_matcher.py — tolerant keyword matching for chat messages. Qt-free.

Offender chat is full of non-standard spelling (Lorenzo-Dus & Kinzel 2019
had to normalise their corpus before analysing it), so a term matches its
common disguises as well as itself. Text and terms are reduced to the same
canonical form, then matched by lookups (fast enough for every message on
a phone, unlike one search pattern per term):

  numbers/symbols for letters   p3d0, $ex, h0rny        -> pedo, sex, horny
  stretched letters             pleeease, nuuudes        -> please, nudes
  spaced-out letters            s.e.x, m-d-m-a, p e d o  -> sex, mdma, pedo
  apostrophes                   don't                    -> dont
  one-letter typo               xanex, ketamin           -> xanax, ketamine
      only for distinctive terms of 6+ letters, and never for a word the
      caller lists as ordinary vocabulary (e.g. words used across many of
      the phone's own chats), so "baked" never matches "naked".

Every hit says how it matched, so a hit can be explained in a statement.
"""

import re
import unicodedata

_LEET = str.maketrans({"0": "o", "1": "i", "!": "i", "|": "i", "l": "i", "3": "e", "4": "a",
                       "@": "a", "5": "s", "$": "s", "7": "t", "+": "t", "8": "b", "9": "g"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9@$!|+'’À-ɏ]+")
_EDGE_RE = re.compile(r"^[^A-Za-z0-9À-ɏ]+|[^A-Za-z0-9À-ɏ]+$")
_RUN_RE = re.compile(r"(.)\1+")
_STRETCH_RE = re.compile(r"(.)\1{2,}")      # 3+ identical letters = stretched
_MAX_PHRASE = 6
# Common text-speak, also tried as the full word ("how old r u").
_TEXTSPEAK = {"u": "you", "r": "are", "ur": "your", "yr": "your", "ya": "you",
              "pls": "please", "plz": "please", "pic": "picture", "pix": "pics"}
_TEXTSPEAK = {k: v.replace("'", "") for k, v in _TEXTSPEAK.items()}


_english: set | None = None


def _english_words() -> set:
    """Webster's Second (1934, public domain; config/english_words.txt.gz),
    loaded once. Used only to stop a real word counting as a typo."""
    global _english
    if _english is None:
        import gzip
        import os
        import sys
        base = (os.path.join(sys._MEIPASS, "config") if getattr(sys, "frozen", False)
                and getattr(sys, "_MEIPASS", None) else
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"))
        try:
            with gzip.open(os.path.join(base, "english_words.txt.gz"), "rt", encoding="utf-8") as f:
                _english = {w.strip() for w in f if w and not w.startswith("#")}
        except OSError:
            _english = set()
    return _english


def is_english_word(word: str) -> bool:
    """True if *word* (lower case) or its stem without a common ending is in
    the dictionary: named -> name, cooked -> cook, kidding -> kid."""
    words = _english_words()
    if word in words:
        return True
    for suffix, repl in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"),
                         ("es", ""), ("s", ""), ("er", ""), ("ers", ""), ("ly", ""), ("est", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: len(word) - len(suffix)] + repl
            if stem in words or (len(stem) > 3 and stem[-1] == stem[-2] and stem[:-1] in words):
                return True
    return False


def _canon_token(tok: str) -> str:
    """Canonical form of one token (letters, digits, leet symbols)."""
    tok = unicodedata.normalize("NFKC", tok).lower().replace("’", "'")
    tok = _EDGE_RE.sub("", tok).replace("'", "")
    if not tok:
        return ""
    if any(c.isalpha() for c in tok):        # leave pure numbers ("62", "777") alone
        tok = tok.translate(_LEET)
    return tok


def _variants(c: str) -> list:
    """A text token as written, plus its forms with each run of 3+ identical
    letters read as one or two letters ("pleeease" -> please / pleease,
    "asss" -> as / ass). Terms are never collapsed, so the word "as" can
    never match the term "ass"."""
    if c.isdigit():
        return [c]
    found = [c]
    runs = list(_STRETCH_RE.finditer(c))
    if runs:
        out = [""]
        pos = 0
        for m in runs[:4]:
            mid = c[pos:m.start()]
            out = [o + mid + m.group(1) * k for o in out for k in (1, 2)]
            pos = m.end()
        found += [o + c[pos:] for o in out]
    single = _RUN_RE.sub(r"\1", c)       # "pedoo" -> pedo (text only)
    if single != c:
        found.append(single)
    if c in _TEXTSPEAK:
        found.append(_TEXTSPEAK[c])
    out, seen = [], set()
    for v in found:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out[1:] + out[:1]          # the form as written last (used for typos)


def _canon_phrase(text: str) -> list:
    """Canonical words of a TERM (never collapsed; see _variants)."""
    return [c for c in (_canon_token(t) for t in _TOKEN_RE.findall(text)) if c]


def _is_symbolic(term: str) -> bool:
    return not any(ch.isalnum() for ch in term)


def _deletes(word: str) -> set:
    return {word[:i] + word[i + 1:] for i in range(len(word))}


def _within_one_edit(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance <= 1."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    if la > lb:
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def _how(raw: str, term: str, spaced: bool, typo: bool) -> str:
    """Plain-English reason a hit matched, for the report and a statement."""
    r = raw.lower().replace("’", "'")
    t = term.lower().rstrip("*")
    if typo:
        rc, tc = _canon_token(r), _canon_token(t)
        if len(rc) == len(tc) and sorted(rc) == sorted(tc):
            return "two letters swapped"
        return "one letter different"
    if r == t or (term.endswith("*") and r.startswith(t)):
        return "exact"
    ways = []
    if spaced:
        ways.append("spaced out")
    if any(c in r for c in "0134578@$!|+9") and not any(c in t for c in "0134578@$!|+9"):
        ways.append("numbers/symbols for letters")
    if _RUN_RE.sub(r"\1", r) == _RUN_RE.sub(r"\1", t) and r != t and len(r) > len(t):
        ways.append("stretched letters")
    if any(w in _TEXTSPEAK for w in re.findall(r"[a-z]+", r)) and \
            not any(w in _TEXTSPEAK for w in re.findall(r"[a-z]+", t)):
        ways.append("text-speak")
    if "'" in t and "'" not in r:
        ways.append("no apostrophe")
    return ", ".join(ways) or "same word, different form"


class Matcher:
    """Compiled set of terms. find(text) -> list of hits:
    {term (the term dict), matched (text as written), how, start, end}."""

    def __init__(self, terms: list, ordinary_words=None):
        self.ordinary = {_canon_token(w) for w in (ordinary_words or ())}
        self.single: dict = {}       # canonical word -> [term]
        self.stems: list = []        # (canonical prefix, term)
        self.phrases: dict = {}      # tuple of canonical words -> [term]
        self.symbols: list = []      # (raw string, term) for emoji / symbol-only terms
        self.typo_index: dict = {}   # deletion variant -> {canonical word}
        self.typo_terms: dict = {}   # canonical word -> [term]
        for t in terms:
            raw = (t.get("term") or "").strip()
            if not raw:
                continue
            if _is_symbolic(raw):
                self.symbols.append((raw, t))
                continue
            stem = raw.endswith("*")
            words = _canon_phrase(raw.rstrip("*"))
            if not words:
                continue
            if stem and len(words) == 1:
                self.stems.append((words[0], t))
            elif len(words) == 1:
                self.single.setdefault(words[0], []).append(t)
                w = words[0]
                if (not t.get("everyday") and len(w) >= 5 and w.isalpha()):
                    self.typo_terms.setdefault(w, []).append(t)
                    for d in _deletes(w) | {w}:
                        self.typo_index.setdefault(d, set()).add(w)
            else:
                self.phrases.setdefault(tuple(words[:_MAX_PHRASE]), []).append(t)
        self.max_len = max((len(k) for k in self.phrases), default=1)

    def _tokens(self, text: str) -> list:
        """[(canonical, start, end, spaced)] with spaced-out letter runs
        (s.e.x / p e d o) joined into one extra token."""
        toks = []
        for m in _TOKEN_RE.finditer(text):
            c = _canon_token(m.group(0))
            if c:
                toks.append((_variants(c), m.start(), m.end(), False))
        out = list(toks)
        i = 0
        while i < len(toks):
            j = i
            while j < len(toks) and len(toks[j][0][-1]) == 1 and \
                    (j == i or toks[j][1] - toks[j - 1][2] <= 2):
                j += 1
            if j - i >= 3:
                joined = "".join(t[0][-1] for t in toks[i:j])
                out.append(([joined], toks[i][1], toks[j - 1][2], True))
                i = j
            else:
                i += 1
        return out

    def find(self, text: str) -> list:
        if not text:
            return []
        hits, seen = [], set()

        def add(t, start, end, spaced=False, typo=False):
            k = (id(t), start)
            if k in seen:
                return
            seen.add(k)
            raw = text[start:end]
            hits.append({"term": t, "matched": raw, "start": start, "end": end,
                         "how": _how(raw, t["term"], spaced, typo)})

        for raw, t in self.symbols:
            i = text.find(raw)
            if i >= 0:
                add(t, i, i + len(raw))
        toks = self._tokens(text)
        plain = [x for x in toks if not x[3]]
        for variants, s, e, spaced in toks:
            for c in variants:
                for t in self.single.get(c, ()):
                    add(t, s, e, spaced)
                for prefix, t in self.stems:
                    if c.startswith(prefix):
                        add(t, s, e, spaced)
            c = variants[-1]
            if (c not in self.single and len(c) >= 5 and c.isalpha()
                    and c not in self.ordinary and self.typo_index
                    and not is_english_word(_EDGE_RE.sub("", text[s:e].lower()).replace("’", "'"))):
                cands = set()
                for d in _deletes(c) | {c}:
                    cands |= self.typo_index.get(d, set())
                for w in cands:
                    # 6+ letters: any one edit; 5 letters: only two swapped
                    # letters (a substitution there too often makes a real
                    # word: naked/baked)
                    ok = (_within_one_edit(w, c) if len(w) >= 6
                          else len(w) == len(c) and sorted(w) == sorted(c)
                          and _within_one_edit(w, c))
                    if w != c and ok:
                        for t in self.typo_terms[w]:
                            add(t, s, e, spaced, typo=True)
        if self.phrases:
            import itertools
            for i in range(len(plain)):
                for n in range(2, min(self.max_len, len(plain) - i) + 1):
                    span = [x[0] for x in plain[i:i + n]]
                    if all(len(v) == 1 for v in span):
                        combos = [tuple(v[0] for v in span)]
                    else:
                        combos = itertools.islice(itertools.product(*span), 16)
                    for combo in combos:
                        for t in self.phrases.get(combo, ()):
                            add(t, plain[i][1], plain[i + n - 1][2])
        hits.sort(key=lambda h: h["start"])
        return hits
