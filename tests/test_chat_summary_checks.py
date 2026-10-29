"""Per-chat AI summary: the model fills a fixed checklist; the app checks
every finding in code (number exists, speaker sent it, quoted words are in
it, number corrected when the words are in another message), works out
media itself, and runs its own word scan."""
import os

import ai_summary as a

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


def test_media_facts_and_keyword_hits():
    assert a.media_facts(MSGS) == [('Me', ['#4', '#6 (type 5)'])]
    hits = dict(a.watch_word_hits(MSGS))
    labels = ' | '.join(hits)
    assert (1, 'how old are you', 'exact') in hits['Grooming / sexual exploitation: Age / risk checks / sexual experience'], labels
    assert any(n == 3 for n, _, _ in hits['Grooming / sexual exploitation: Pictures / camera']), labels
    # "send one without your hoodie" (#5) is a paraphrase no research list covers:
    # the meaning layer, not the keyword lists, is meant to catch it
    assert not any(n == 5 for found in hits.values() for n, _, _ in found)
    assert not any(n == 7 for found in hits.values() for n, _, _ in found)   # "moment" is not "mom"


def test_system_only_chat_needs_no_model():
    assert not a.has_content([{'speaker': 'x', 'text': '[No text — message type 6]', 'media': False}])
    assert a.has_content(MSGS)


def test_focus_sends_only_the_session_but_keeps_numbers(monkeypatch):
    sent = {}

    def fake_call(endpoint, key, model, prompt, **kw):
        sent['prompt'] = prompt
        return {'text': '{"asked_for_pictures": [], "meet_or_call": [], "delete_hide_or_dont_tell": [],'
                        ' "sex_age_money_drugs_threats": [], "topics": "x"}'}
    monkeypatch.setattr(a.local_llm, 'call_chat', fake_call)
    r = a.summarise_chat(MSGS, {'endpoint': '', 'api_key': '', 'model': 'm'}, a.CHAT_PROMPT, focus=(3, 5))
    assert 'calls' in r
    assert '#3 [' in sent['prompt'] and '#5 [' in sent['prompt']
    assert '#1 [' not in sent['prompt'] and '#6 [' not in sent['prompt']


def test_report_renders_with_and_without_focus():
    checked = a.check_findings({k: [] for k, _ in a._FINDING_LISTS}, MSGS)
    full = a.render_chat_report('Alex', MSGS, checked, 'm', 'now', 1, 1.0)
    part = a.render_chat_report('Alex', MSGS, checked, 'm', 'now', 1, 1.0, focus=(3, 5))
    assert 'all 7 message(s) sent' in full
    assert 'messages #3–#5 sent (the strongest red-flag session of 7)' in part


def test_report_labels_reach_the_model_in_brackets_and_never_as_text():
    """Event is the parser's own label. The model sees it in brackets when a
    row has no text; it is never in m["text"], which the quote check reads."""
    import ai_summary
    call = {"text": "", "event": "Video call, Not answered", "media": False}
    assert ai_summary._prompt_text(call) == "[Video call, Not answered]"
    typed = {"text": "video call later?", "event": "", "media": False}
    assert ai_summary._prompt_text(typed) == "video call later?"
    ios59 = {"text": "", "event": "System event, type 59 — adjacent to a call", "media": False}
    assert ai_summary._prompt_text(ios59) == "[call event]"
    card = {"text": "", "event": "No text — message type 5", "media": True, "speaker": "Me"}
    assert ai_summary.media_facts([card]) == [("Me", ["#1 (type 5)"])]


def test_chat_parsers_keep_their_own_words_out_of_the_message():
    """Every chat parser that counts calls reads a separate column, and none
    finds media by a prefix the parser wrote into the text."""
    import importlib.util
    for platform, name in (("android", "whatsapp"), ("android", "groupme"), ("android", "burner"),
                           ("android", "line"), ("android", "viber")):
        path = os.path.join(ROOT, "artifacts", platform, f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"{platform}_{name}_decl", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        decl = mod.chat_groups
        assert decl.get("call_field") == "event", name
        assert not decl.get("media_prefixes"), name


def test_chat_parsers_with_system_rows_declare_a_system_direction():
    """App events (WhatsApp group lines, LINE type 33, iOS WhatsApp type 6)
    are Direction System, so Chat Groups and the AI never count them as sent."""
    import importlib.util
    for platform, name in (("android", "whatsapp"), ("android", "line"),
                           ("android", "groupme"), ("ios", "whatsapp")):
        path = os.path.join(ROOT, "artifacts", platform, f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"{platform}_{name}_sys", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "System" in mod.chat_groups.get("system", ()), (platform, name)
