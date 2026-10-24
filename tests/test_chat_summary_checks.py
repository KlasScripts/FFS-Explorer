"""Per-chat AI summary: the model fills a fixed checklist; the app checks
every finding in code (number exists, speaker sent it, quoted words are in
it, number corrected when the words are in another message), works out
media itself, and runs its own word scan."""
import ai_summary as a

MSGS = [
    {'speaker': 'Alex', 'time': '2024-03-01 19:06:00 UTC', 'text': 'how old are you btw', 'media': False},
    {'speaker': 'Me', 'time': '2024-03-01 19:07:00 UTC', 'text': '14', 'media': False},
    {'speaker': 'Alex', 'time': '2024-03-01 19:08:00 UTC', 'text': 'cool. you look older. send me a pic of you',
     'media': False},
    {'speaker': 'Me', 'time': '2024-03-01 19:12:00 UTC',
     'text': '[Media attachment — no caption text stored in this table]', 'media': True},
    {'speaker': 'Alex', 'time': '2024-03-01 19:13:00 UTC', 'text': 'send one without your hoodie', 'media': False},
    {'speaker': 'Me', 'time': '2024-03-01 19:14:00 UTC', 'text': '[No text — message type 5]', 'media': True},
    {'speaker': 'Me', 'time': '2024-03-01 19:15:00 UTC', 'text': 'Just a moment', 'media': False},
]


def _one(key, who, what, nums):
    f = {k: [] for k, _ in a._FINDING_LISTS}
    f[key] = [{'who': who, 'what': what, 'messages': nums}]
    return a.check_findings(f, MSGS)[key][0]


def test_prompt_lines_are_cleaned():
    lines = a.format_chat_lines(MSGS)
    assert lines[3] == '#4 [2024-03-01 19:12] Me: [MEDIA]'
    assert lines[5] == '#6 [2024-03-01 19:14] Me: [MEDIA, type 5]'


def test_good_finding_passes():
    it = _one('asked_for_pictures', 'Alex', 'send me a pic of you', [3])
    assert it['problems'] == [] and it['messages'] == [3]


def test_wrong_number_is_corrected_from_the_quote():
    it = _one('sex_age_money_drugs_threats', 'Alex', 'you look older', [2])
    assert it['messages'] == [3] and 'corrected from #2 to #3' in it['note']


def test_wrong_speaker_and_missing_quote_are_flagged():
    it = _one('asked_for_pictures', 'Me', 'how old are you', [1])
    assert any('none of the cited messages is from Me' in p for p in it['problems'])
    it = _one('meet_or_call', 'Alex', 'meet at the park', [3])
    assert 'quoted words not found in the cited message(s)' in it['problems']
    it = _one('meet_or_call', 'Alex', 'x', [99])
    assert any('#99, not in this chat' in p for p in it['problems'])


def test_media_facts_and_word_scan():
    assert a.media_facts(MSGS) == [('Me', ['#4', '#6 (type 5)'])]
    hits = dict(a.watch_word_hits(MSGS))
    assert (5, 'send') in hits['Pictures / media']          # the request the model missed
    assert (1, 'how old') in hits['Age / school / family']
    assert not any(n == 7 for n, _ in hits.get('Age / school / family', []))   # "moment" is not "mom"


def test_system_only_chat_needs_no_model():
    assert not a.has_content([{'speaker': 'x', 'text': '[No text — message type 6]', 'media': False}])
    assert a.has_content(MSGS)
