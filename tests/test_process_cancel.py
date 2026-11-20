"""Process Case's Cancel asks first (ProcessDialog._on_cancel):
"Skip this stage" stops only the stage running when Cancel was pressed (not
the integrity hash beside it), and the next stage then starts uncancelled;
"Stop all stages" stops every worker, the hash too, and no further stage
starts; "Keep running" changes nothing. Before 2026-11-20 one press stopped
the stage and the hash and the later stages started anyway, and duplicate
detection was never told to stop."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def PD():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    spec = importlib.util.spec_from_file_location('ffs_explorer_main', os.path.join(ROOT, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.ProcessDialog


class Worker:
    def __init__(self, running=True):
        self.running = running
        self.interrupted = False

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.interrupted = True


class Label:
    def __init__(self):
        self.t = ''

    def text(self):
        return self.t

    def setText(self, t):
        self.t = t


class Btn:
    enabled = True

    def setEnabled(self, v):
        self.enabled = v


def _dialog(PD, choice, **workers):
    names = ('_scan_worker', '_nested_worker', '_embedded_media_worker',
             '_media_metadata_worker', '_dedup_worker', '_integrity_worker')
    attrs = {n: workers.get(n) for n in names}
    attrs.update(
        _scan_cancelled=False, _cancel_all=False, _integrity_cancelled=False,
        _waiting_for_case_open=False, _leveldb_indexing_active=False,
        _status_label=Label(), _cancel_btn=Btn(), finished=0, asked=[])
    for meth in ('_on_cancel', '_running_stage', '_stop_all_requested'):
        attrs[meth] = getattr(PD, meth)

    def ask(self, stage_name):
        self.asked.append(stage_name)
        return choice

    def finish(self):
        self.finished += 1
    attrs['_ask_cancel_choice'] = ask
    attrs['_finish_operations'] = finish
    return type('FakeProcessDialog', (), attrs)()


def test_skip_this_stage_leaves_the_hash_and_the_next_stage(PD):
    sweep, hash_ = Worker(), Worker()
    d = _dialog(PD, 'stage', _embedded_media_worker=sweep, _integrity_worker=hash_)
    d._on_cancel()
    assert d.asked == ['embedded-media sweep']
    assert sweep.interrupted and not hash_.interrupted
    assert d._scan_cancelled and not d._cancel_all
    assert d._stop_all_requested() is False          # the next stage starts...
    assert d._scan_cancelled is False and d.finished == 0   # ...uncancelled


def test_stop_all_stops_everything(PD):
    dedup, hash_ = Worker(), Worker()
    d = _dialog(PD, 'all', _dedup_worker=dedup, _integrity_worker=hash_)
    d._on_cancel()
    assert d.asked == ['duplicate detection']
    assert dedup.interrupted and hash_.interrupted and d._integrity_cancelled
    assert d._cancel_all and not d._cancel_btn.enabled
    assert d._stop_all_requested() is True and d.finished == 1
    assert 'remaining stages were not run' in d._status_label.text()


def test_keep_running_changes_nothing(PD):
    scan = Worker()
    d = _dialog(PD, None, _scan_worker=scan)
    d._on_cancel()
    assert not scan.interrupted and not d._scan_cancelled and not d._cancel_all


def test_only_the_hash_left_is_the_stage(PD):
    hash_ = Worker()
    d = _dialog(PD, 'stage', _integrity_worker=hash_)
    d._on_cancel()
    assert d.asked == ['data integrity hash']
    assert hash_.interrupted and d._integrity_cancelled and not d._scan_cancelled


def test_stage_that_ended_during_the_question_is_not_cancelled(PD):
    scan = Worker()
    d = _dialog(PD, 'stage', _scan_worker=scan)

    def ask(stage_name):
        scan.running = False                         # ended while the box was open
        return 'stage'
    d._ask_cancel_choice = ask
    d._on_cancel()
    assert not scan.interrupted and not d._scan_cancelled


def test_cancel_while_waiting_for_case_open_needs_no_question(PD):
    d = _dialog(PD, 'stage')
    d._waiting_for_case_open = True
    d._on_cancel()
    assert d.asked == [] and d._cancel_all
