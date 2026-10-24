"""The per-chat AI summary checks are done in code, not trusted to the
model: every point must cite real message numbers, and a point naming a
speaker must cite at least one of their messages."""
from ai_summary import _short_speaker, check_chat_points, format_chat_lines

MSGS = [
    {'speaker': 'Liz Dehner', 'time': '2024-01-30 18:37:03 UTC', 'text': 'Hi there!', 'media': False},
    {'speaker': 'Me', 'time': '2024-01-30 19:10:55 UTC', 'text': "I'm good.", 'media': False},
    {'speaker': 'Liz Dehner', 'time': '2024-01-30 20:01:51 UTC', 'text': 'Here comes a video call.',
     'media': False},
]


def _flags(text):
    return check_chat_points(text, MSGS)


def test_lines_are_numbered_with_short_times():
    assert format_chat_lines(MSGS)[0] == '#1 [2024-01-30 18:37] Liz Dehner: Hi there!'


def test_good_points_pass():
    out, points, flagged, cited = _flags("SUMMARY:\n- Liz Dehner said hi [#1]\n- The phone owner was good [#2]")
    assert (points, flagged, cited) == (2, 0, [1, 2]) and '⚠' not in out


def test_wrong_speaker_missing_and_invalid_citations_are_flagged():
    # the real qwen3-vl-8b mistake on IOS17: crediting Liz's video call to Me
    out, points, flagged, _ = _flags("- Me started a video call [#3]\n- Liz waved\n- Liz Dehner left [#9]")
    assert (points, flagged) == (3, 3)
    assert 'names Me' in out and 'no message cited' in out and '#9, not in this chat' in out


def test_ranges_and_jid_suffix():
    _, _, _, cited = _flags("- Liz Dehner chatted [#1–#3]")
    assert cited == [1, 2, 3]
    assert _short_speaker('Liz Dehner (19199282177@s.whatsapp.net)') == 'Liz Dehner'
