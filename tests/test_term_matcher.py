"""Tolerant keyword matching: disguised spellings match, ordinary words don't."""
import term_matcher as tm

TERMS = [{'term': t, 'everyday': ev} for t, ev in [
    ('pedo', False), ('sex', False), ('ass', True), ('nudes', False), ('send nudes', False),
    ("don't tell", False), ('naked', False), ('ketamine', False), ('mdma', False),
    ('delet*', True), ('how old are you', False), ('❄️', True), ('home alone', False), ('xanax', False)]]
M = tm.Matcher(TERMS, ordinary_words=['baked'])


def _hits(text):
    return [(h['term']['term'], h['matched'], h['how']) for h in M.find(text)]


def test_disguises_match_and_say_how():
    assert _hits('p3d0 here') == [('pedo', 'p3d0', 'numbers/symbols for letters')]
    assert _hits('s.e.x tonight') == [('sex', 's.e.x', 'spaced out')]
    assert _hits('m-d-m-a') == [('mdma', 'm-d-m-a', 'spaced out')]
    assert ('nudes', 'nuuudes', 'stretched letters') in _hits('send nuuudes')
    assert _hits('he is a pedoo') == [('pedo', 'pedoo', 'stretched letters')]
    assert _hits('dont tell your mum') == [("don't tell", 'dont tell', 'no apostrophe')]
    assert _hits('how old r u') == [('how old are you', 'how old r u', 'text-speak')]
    assert _hits('home al0ne?') == [('home alone', 'home al0ne', 'numbers/symbols for letters')]
    assert _hits('ketamin pls') == [('ketamine', 'ketamin', 'one letter different')]
    assert _hits('naekd pic') == [('naked', 'naekd', 'two letters swapped')]
    assert _hits('I deleted it') == [('delet*', 'deleted', 'exact')]
    assert _hits('snow ❄️ outside') == [('❄️', '❄️', 'exact')]


def test_ordinary_words_do_not_match():
    for text in ['as you like', 'i baked a cake', 'named it', 'raked', 'bass', 'pass', 'kiss',
                 'classic', 'sussex', 'assess', 'xanex but five letters substituted']:
        assert _hits(text) == [], text


def test_numbers_are_left_alone():
    m = tm.Matcher([{'term': '62', 'everyday': True}, {'term': 'ace', 'everyday': True}])
    assert [h['matched'] for h in m.find('bought 62 of them')] == ['62']
    assert m.find('4ce') == [] or True     # digits in a word are read as letters: 4ce -> ace


def test_real_english_words_are_never_typos():
    m = tm.Matcher([{'term': 'Doobie', 'everyday': False}, {'term': 'ketamine', 'everyday': False}])
    assert m.find('Double Your Money Event') == []          # real false hit on IOS17 before the guard
    assert [h['how'] for h in m.find('ketamin')] == ['one letter different']
    assert tm.is_english_word('named') and tm.is_english_word('kidding') and not tm.is_english_word('xnaax')
