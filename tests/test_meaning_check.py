"""Meaning check maths (no model needed): Sentinel-style contrastive score
and skewness, with the chat's own messages left out of the harmless side."""
import math

import meaning_check as mc


def _v(*xs):
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


def test_score_is_positive_only_when_closer_to_harmful():
    harmful = [_v(1, 0, 0)]
    harmless = [_v(0, 1, 0), _v(0, 0, 1), _v(0, 1, 1)]
    s, j = mc.score_vector(_v(1, 0.1, 0), harmful, harmless)
    assert s > mc.MIN_SCORE and j == 0
    s, _ = mc.score_vector(_v(0, 1, 0.1), harmful, harmless)
    assert s == 0.0


def test_own_chat_is_left_out_of_harmless_side():
    harmful = [_v(1, 0, 0)]
    harmless = [_v(1, 0.05, 0), _v(0, 1, 0), _v(0, 0, 1), _v(0, 1, 1)]   # index 0 = the chat's own message
    msg = _v(1, 0.05, 0)
    with_own = mc.score_vector(msg, harmful, harmless)[0]
    without_own = mc.score_vector(msg, harmful, harmless, skip={0})[0]
    assert without_own > with_own                                      # its own copy no longer dilutes it


def test_skewness():
    assert mc.skewness([0.0] * 5) is None
    assert mc.skewness([0.0] * 12) == 0.0
    assert mc.skewness([0.0] * 11 + [1.0]) > 2          # mostly normal, one strong outlier


def test_bank_has_sources():
    bank = mc.load_bank()
    assert len(bank['examples']) >= 20
    assert all(e['source'] in bank['sources'] for e in bank['examples'])
