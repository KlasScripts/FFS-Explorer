"""Baseline keyword lists ship with the app; the examiner's terms and on/off
choices are kept separately and marked as theirs."""
import json
import sys

import keyword_lists as kl


def _frozen(tmp_path, monkeypatch, baseline_terms):
    internal = tmp_path / 'app' / '_internal' / 'config'
    internal.mkdir(parents=True)
    (internal / 'keyword_list_grooming.json').write_text(json.dumps(
        {'list': 'grooming', 'sources': {'LDK2019': 'paper'}, 'terms': baseline_terms}))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'app' / '_internal'), raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'app' / 'ffs-explorer.exe'))
    kl._cache.clear()


BASE = [{'term': 'down there', 'category': 'Sexual words', 'source': 'LDK2019', 'enabled': True},
        {'term': 'trust me', 'category': 'Flattery / love', 'source': 'BLACK2015', 'enabled': False}]


def test_user_terms_and_choices_are_kept_apart_from_the_baseline(tmp_path, monkeypatch):
    _frozen(tmp_path, monkeypatch, BASE)
    assert [t['origin'] for t in kl.load('grooming')] == ['baseline', 'baseline']
    kl.add_term('grooming', 'snapchat me', 'Secrecy / deleting', added_by='examiner')
    kl.add_term('grooming', 'sned nudes', 'Pictures / camera', origin='llm', model='qwen')
    kl.set_enabled('grooming', 'down there|Sexual words', False)
    terms = {t['term']: t for t in kl.load('grooming')}
    assert terms['snapchat me']['origin'] == 'user' and terms['sned nudes']['model'] == 'qwen'
    assert terms['down there']['enabled'] is False
    # an upgrade brings a new baseline term; the examiner's changes survive
    (tmp_path / 'app' / '_internal' / 'config' / 'keyword_list_grooming.json').write_text(json.dumps(
        {'list': 'grooming', 'terms': BASE + [{'term': 'home alone', 'category': 'Who is around',
                                                 'source': 'BLACK2015', 'enabled': True}]}))
    kl._cache.clear()
    terms = {t['term']: t for t in kl.load('grooming')}
    assert set(terms) == {'down there', 'trust me', 'home alone', 'snapchat me', 'sned nudes'}
    assert terms['down there']['enabled'] is False
    # baseline terms can't be removed, only switched off
    assert kl.remove_added('grooming', 'down there|Sexual words') is False
    assert kl.remove_added('grooming', 'snapchat me|Secrecy / deleting') is True


def test_shipped_lists_load_with_sources():
    for name in kl.LISTS:
        terms = kl.load(name)
        assert terms and all(t['source'] in kl.sources(name) or t['origin'] != 'baseline' for t in terms)
