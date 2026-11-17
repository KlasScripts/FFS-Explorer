"""Red flags kept per report (chat_flags.fingerprint / cache_get / cache_put):
a kept result is used only while everything it depends on is unchanged, and
a chat key keeps its type through the saved JSON."""
import os
import sqlite3

import chat_flags


def _case(tmp_path, n):
    case = str(tmp_path)
    c = sqlite3.connect(os.path.join(case, 'caseresults.db'))
    c.execute('DROP TABLE IF EXISTS artifact_demo')
    c.execute('CREATE TABLE artifact_demo (raw_chat_id, message)')
    c.executemany('INSERT INTO artifact_demo VALUES (?,?)', [(i % 3, f'm{i}') for i in range(n)])
    c.commit()
    c.close()
    return case


def test_kept_flags_follow_the_report(tmp_path):
    case = _case(tmp_path, 10)
    fp = chat_flags.fingerprint(case, 'demo', 'model-a')
    assert chat_flags.cache_get(case, 'demo', fp) is None
    flags = {0: {'score': 3, 'name': 'Test Group'}, 'abc': {'score': 0, 'name': ''}}
    chat_flags.cache_put(case, 'demo', fp, flags)
    assert chat_flags.cache_get(case, 'demo', fp) == flags          # int key stays int
    assert chat_flags.fingerprint(case, 'demo', 'model-a') == fp     # nothing changed
    assert chat_flags.fingerprint(case, 'demo', 'model-b') != fp     # another meaning model
    _case(tmp_path, 11)                                              # the report re-run
    fp2 = chat_flags.fingerprint(case, 'demo', 'model-a')
    assert fp2 != fp and chat_flags.cache_get(case, 'demo', fp2) is None
