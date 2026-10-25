"""Red flags: chats ranked by a pattern (several categories close together,
request followed by media), not by raw keyword counts."""
import chat_flags as cf
import term_matcher as tm


def _m(speaker, text, minute, media=False):
    return {'speaker': speaker, 'text': text, 'media': media, 'ts': 1_700_000_000 + minute * 60,
            'time': f'2024-03-01 19:{minute:02d}:00 UTC'}


T = [{'term': t, 'category': c, 'list': 'grooming', 'everyday': False} for t, c in [
    ('how old are you', 'Age / risk checks / sexual experience'), ('send me a pic', 'Pictures / camera'),
    ("don't tell", 'Secrecy / deleting'), ('delete these messages', 'Secrecy / deleting'),
    ('meet me', 'Location / meeting'), ('your address', 'Location / meeting')]] + \
    [{'term': 'lol', 'category': 'Flattery / love', 'list': 'grooming', 'everyday': False}]

CHATS = {
    'grooming': {'name': 'Alex', 'messages': [
        _m('Alex', 'how old are you btw', 1), _m('Me', '14', 2), _m('Alex', 'cool. send me a pic of you', 3),
        _m('Me', '[Media attachment]', 6, media=True), _m('Alex', "dont tell your mum we talk", 8),
        _m('Alex', 'meet me at the park saturday', 10), _m('Alex', 'delete these messages after ok', 12)]},
    'scam': {'name': 'USPS', 'messages': [
        _m('USPS', 'Your package cannot be delivered, confirm your address here', 1)]},
    'friends': {'name': 'Sam', 'messages': [_m('Sam', 'lol', 1), _m('Me', 'lol ok', 2)]},
}


def test_pattern_outranks_single_hits():
    r = cf.score_chats(CHATS, tm.Matcher(T))
    assert r['grooming']['score'] > r['scam']['score'] > 0
    assert r['grooming']['level'] == '●●●' and r['scam']['level'] == '●○○'
    assert set(r['grooming']['categories']) == {'Age / risk checks / sexual experience', 'Pictures / camera',
                                                 'Secrecy / deleting', 'Location / meeting'}
    assert any('#3 → #4' in x for x in r['grooming']['reasons'])          # request then media
    assert 'too few' in r['grooming']['rarity_note']


def test_terms_hitting_every_chat_count_for_little():
    chats = {f'c{i}': {'name': f'c{i}', 'messages': [_m('x', 'lol', 1)]} for i in range(12)}
    chats['grooming'] = CHATS['grooming']
    r = cf.score_chats(chats, tm.Matcher(T))
    assert r['c0']['score'] < 0.2 and r['grooming']['score'] >= 6
    assert r['c0']['rarity_note'] == ''


def test_report_text_marks_words_and_context():
    r = cf.score_chats(CHATS, tm.Matcher(T))
    text = cf.report_text('Alex', CHATS['grooming']['messages'], r['grooming'])
    assert '»send me a pic«' in text and '▶ #3' in text and 'not a finding' in text
    assert cf.report_text('Sam', [], {'score': 0}).startswith('RED FLAGS — Sam\nNone found')


def test_same_opener_from_me_in_many_chats():
    chats = {f'k{i}': {'name': f'kid {i}', 'messages': [_m('Me', 'hey u from round here? ', 1), _m(f'k{i}', 'yeah', 2)]}
             for i in range(3)}
    chats['spam1'] = {'name': 'x', 'messages': [_m('bot', 'You won a prize click here', 1)]}
    chats['spam2'] = {'name': 'y', 'messages': [_m('bot', 'You won a prize click here', 1)]}
    chats['spam3'] = {'name': 'z', 'messages': [_m('bot', 'You won a prize click here', 1)]}
    r = cf.score_chats(chats, tm.Matcher([]))
    assert r['k0']['score'] == 1.5 and 'same opening message from Me in 3 chats' in r['k0']['reasons'][0]
    assert r['spam1']['score'] == 0


def test_meaning_hits_count_as_a_category():
    chats = {'a': CHATS['grooming'], 'b': CHATS['friends']}
    bank = [{'text': 'send me a picture without your clothes on', 'source': 'OWNER'}]
    meaning = {'_bank': bank, 'a': {'scores': [0, 0, 0, 0, 0, 0, 0.06], 'nearest': [None] * 6 + [0], 'skew': None},
               'b': {'scores': [0, 0], 'nearest': [None, None], 'skew': None}}
    r = cf.score_chats(chats, tm.Matcher(T), meaning=meaning)
    assert 'Similar to known grooming messages' in r['a']['categories']
    text = cf.report_text('Alex', CHATS['grooming']['messages'], r['a'])
    assert 'meaning score 0.06, like "send me a picture without your clothes on" (OWNER)' in text
