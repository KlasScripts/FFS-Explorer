"""Baseline and benchmark for the restructure: record what FFS Explorer
produces on the test archives, and how long it takes, so every later change
can be proven to give identical results.

    venv/bin/python scripts/baseline.py run --out ~/script/ffs-baseline --run r1
    venv/bin/python scripts/baseline.py run --out ... --run r2   (same code again)
    venv/bin/python scripts/baseline.py compare ~/script/ffs-baseline r1 r2

A run drives the real code paths, each stage in its own process:
  open-cold   the main window (offscreen) opens the archive with no case cache
  session     a fresh process reopens it (warm), runs Process Case with every
              stage except nested-archive selection, then the keyword searches
  parsers     every report for the platform, through run_artifact
  dump        the file index, per-file metadata, folder data, every table in
              casecache.db and caseresults.db, the files Process Case wrote,
              and the report rows, each as canonical JSON lines

Outputs go under <out>/<archive>/<run>/; nothing is written to the repository
or to an archive. Personal case data is not part of ARCHIVES.

compare reports, per output, equal or the differences. Two runs of the same
code give the volatile fields (run times, temp paths); compare lists any
difference, and volatile.json in <out> names the fields to leave out, with
the reason, so nothing is excluded silently.
An archive whose run is missing, or has a step that failed or never ran, or
a stage that did not finish ("ok": false, e.g. a timed-out Process Case), is
reported NOT COMPARED, and the result is INCOMPLETE (exit 2), never
IDENTICAL; DIFFERENT exits 1, IDENTICAL 0.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The test archives' folder; FFS_TEST_DATA points elsewhere (e.g. the PC's
# \\Mac\test data share; "/" in the names below works on Windows too).
TEST = os.environ.get("FFS_TEST_DATA", "/Users/klastveita/script/test data")
ARCHIVES = {
    "IOS17_JoshHickman": f"{TEST}/IOS17 JoshHickman/EXTRACTION_FFS.zip",
    "IOS18_CTF25_Magnet": f"{TEST}/IOS18 CTF25 Magnet/00008110-0008196A2299401E_files_full.zip",
    "iOS16.5_CTF23_Cellebrite": f"{TEST}/iOS16.5 CTF23 Cellebrite/EXTRACTION_FFS.zip",
    "Android14_JoshHickman": f"{TEST}/Android 14 JoshHickman/EXTRACTION_FFS.zip",
    "Android15_CTF25_Cellebrite": f"{TEST}/Android 15 CTF25 Cellebrite/EXTRACTION_FFS.zip",
    "Android14_CTF26_Magnet": f"{TEST}/Android 14 CTF26 Magnet/R5CW32F0PQH_files_full.zip",
}
SEARCH_TERMS = ("password", "whatsapp", "@gmail.com")
BLOB_INLINE_MAX = 4096          # larger BLOBs are recorded by SHA-256
STAGE_TIMEOUT_S = 6 * 3600
INTERPRET_MAX = 5000            # see question() in _gui


def _paths():
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "app"))


# ── canonical output ─────────────────────────────────────────────────────────

_MHTML_BOUNDARY = re.compile(rb"MultipartBoundary--[0-9a-f]+")
_TMP_RE = re.compile(r"(/private)?/var/folders/[^\s'\"]+|/tmp/[^\s'\"]+")


def _norm(value, case_dir):
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value)
        if len(value) > BLOB_INLINE_MAX:
            return {"blob_sha256": hashlib.sha256(value).hexdigest(), "len": len(value)}
        return {"hex": value.hex()}
    if isinstance(value, str):
        if case_dir:
            value = value.replace(case_dir, "<CASE>")
        return _TMP_RE.sub("<TMP>", value)
    if isinstance(value, dict):
        return {str(k): _norm(v, case_dir) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_norm(v, case_dir) for v in value]
        return sorted(items, key=_key) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, float) and value != value:
        return "NaN"
    return value if value is None or isinstance(value, (int, float, bool)) else repr(value)


def _key(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


class Writer:
    """One output: canonical JSON lines, gzip, plus a digest of the sorted lines."""

    def __init__(self, folder, name, case_dir):
        self.path = os.path.join(folder, name + ".jsonl.gz")
        self.case_dir = case_dir
        self.lines = []

    def add(self, row):
        self.lines.append(_key(_norm(row, self.case_dir)))

    def close(self):
        self.lines.sort()
        h = hashlib.sha256()
        with gzip.open(self.path, "wt", encoding="utf-8") as f:
            for line in self.lines:
                f.write(line + "\n")
                h.update(line.encode("utf-8") + b"\n")
        return {"rows": len(self.lines), "sha256": h.hexdigest()}


# ── GUI stages (each run in its own process) ─────────────────────────────────

def _gui(zip_path, case_dir, mode, result_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    _paths()
    import importlib.util
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    QDialog.exec = lambda self, *a, **k: 0          # no modal dialog blocks a run
    QMessageBox.exec = lambda self, *a, **k: 0
    shown = []                                      # every message the app showed, recorded

    def box(kind, answer):
        def f(parent, title, text, *a, **k):
            shown.append({"kind": kind, "title": title, "text": text})
            return answer
        return staticmethod(f)
    QMessageBox.warning = box("warning", QMessageBox.StandardButton.Ok)
    QMessageBox.information = box("information", QMessageBox.StandardButton.Ok)
    QMessageBox.critical = box("critical", QMessageBox.StandardButton.Ok)
    # Questions are answered Yes, except "Interpret SQL/WAL Hits?" over
    # INTERPRET_MAX SQLite hits: the app interprets one hit at a time, and
    # 100,000+ hits would run for hours. The answer is recorded with the text.
    def question(parent, title, text, *a, **k):
        answer = QMessageBox.StandardButton.Yes
        m = re.match(r"\s*([\d,]+) of these", text or "")
        if title.startswith("Interpret SQL") and m and int(m.group(1).replace(",", "")) > INTERPRET_MAX:
            answer = QMessageBox.StandardButton.No
        shown.append({"kind": "question", "title": title, "text": text,
                      "answer": "Yes" if answer == QMessageBox.StandardButton.Yes else "No"})
        return answer
    QMessageBox.question = staticmethod(question)
    from PySide6.QtWidgets import QFileDialog, QInputDialog

    def asked(kind, answer):
        def f(*a, **k):
            shown.append({"kind": kind, "args": [str(x) for x in a[1:3]]})
            return answer
        return staticmethod(f)
    QFileDialog.getExistingDirectory = asked("choose folder", "")
    QFileDialog.getOpenFileName = asked("open file", ("", ""))
    QFileDialog.getSaveFileName = asked("save file", ("", ""))
    QInputDialog.getText = asked("input text", ("", False))
    QInputDialog.getItem = asked("input item", ("", False))
    spec = importlib.util.spec_from_file_location("ffs_explorer_main", os.path.join(ROOT, "ffs-explorer.py"))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    result = {"mode": mode, "stages": {}, "notes": []}
    w = main.FastZipBrowser()
    w._get_or_ask_case_dir = lambda p: case_dir
    for name in ("_open_process_dialog_for_new_case",):
        if hasattr(w, name):
            setattr(w, name, lambda *a, **k: None)

    gaps, last = [], [time.monotonic()]

    def beat():
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now
    hb = QTimer()
    hb.timeout.connect(beat)
    hb.start(20)

    def wait(cond, limit):
        start = time.monotonic()
        loop = QEventLoop()
        t = QTimer()
        t.timeout.connect(lambda: loop.quit() if cond() or time.monotonic() - start > limit else None)
        t.start(50)
        loop.exec()
        t.stop()
        return cond()

    def freeze(since):
        g = [x for x in gaps[since:]]
        return {"longest_s": round(max(g), 3) if g else 0,
                "over_0_5s": sum(1 for x in g if x > 0.5),
                "total_over_0_1s": round(sum(x for x in g if x > 0.1), 2)}

    # Open.
    g0 = len(gaps)
    t0 = time.monotonic()
    w.start_loading(zip_path)
    ok = wait(lambda: getattr(w, "_tree_done", False) and getattr(w, "_worker_done", False), 3600)
    result["stages"]["open"] = {"seconds": round(time.monotonic() - t0, 2), "ok": ok, "ui_freeze": freeze(g0)}

    if mode == "session":
        # Process Case: every stage except nested-archive selection (it needs
        # an examiner's choice). Header scan tier 1, as preselected for a new case.
        stages = result["stages"]
        marks = {}

        def run_dialog(dlg, *a, **k):
            dlg._chk_nested.setChecked(False)
            for chk in ("_chk_integrity", "_chk_leveldb", "_chk_embedded_media",
                        "_chk_media_metadata", "_chk_dedup"):
                getattr(dlg, chk).setChecked(True)
            if dlg._load_current_header_tier() < 1:
                dlg._selected_upgrade_tier = 1
            start = time.monotonic()
            for sig, name in ((dlg.header_scan_done, "header_scan"),
                              (dlg.leveldb_indexing_done, "leveldb_index"),
                              (dlg.embedded_media_scan_done, "embedded_media"),
                              (dlg.media_metadata_scan_done, "media_metadata"),
                              (dlg.duplicate_detection_done, "duplicates")):
                sig.connect(lambda *_a, n=name: marks.setdefault(n, time.monotonic() - start))
            orig_integrity = dlg._on_integrity_done

            def integrity_done(digest, _orig=orig_integrity):
                marks.setdefault("integrity", time.monotonic() - start)
                result["integrity_sha256"] = digest
                return _orig(digest)
            dlg._on_integrity_done = integrity_done
            finished = []
            orig_finish = dlg._finish_operations

            def finish(_orig=orig_finish):
                finished.append(time.monotonic() - start)
                return _orig()
            dlg._finish_operations = finish
            g1 = len(gaps)
            dlg._run_operations()
            done = wait(lambda: bool(finished), STAGE_TIMEOUT_S)
            stages["process_case"] = {"seconds": round(finished[0], 2) if finished else None,
                                      "ok": done, "stage_done_at_s": {k: round(v, 2) for k, v in marks.items()},
                                      "ui_freeze": freeze(g1)}
            return 0
        main.ProcessDialog.exec = run_dialog
        w._open_process_dialog(preselect_tier1=True, preselect_embedded_media=True)

        # Keyword searches, through the main window (scope rules, nested and
        # LevelDB search included).
        for term in SEARCH_TERMS:
            w._skip_search_reminder_once = True
            w.search_field.setText(term)
            g2 = len(gaps)
            t1 = time.monotonic()
            w._start_keyword_search()
            # Done = the search workers, then the SQLite-hit discovery and
            # every interpretation it started (the app asks; the harness says yes).
            def searching():
                disc = getattr(w, "_sql_discovery_worker", None)
                bulk = getattr(w, "_bulk_sql_items", None) or []
                bulk_left = (not getattr(w, "_bulk_sql_cancelled", False)
                             and getattr(w, "_bulk_sql_index", 0) < len(bulk))
                return bool(getattr(w, "_search_pending_workers", None)
                            or (disc is not None and disc.isRunning())
                            or bulk_left
                            or any(x.isRunning() for x in getattr(w, "_sql_interpret_workers", {}).values()))
            done = False
            for _ in range(20):          # quiet for a full second = finished
                if not wait(lambda: not searching(), 3600):
                    break
                calm = time.monotonic()
                wait(lambda: searching() or time.monotonic() - calm > 1.0, 2)
                if not searching():
                    done = True
                    break
            stages[f"search:{term}"] = {"seconds": round(time.monotonic() - t1, 2), "ok": done,
                                        "ui_freeze": freeze(g2)}
            # The results tree as the examiner sees it, interpretations included
            # (they exist only in this tree; nothing else stores them).
            from PySide6.QtCore import Qt
            outputs = os.path.join(os.path.dirname(result_path), "outputs")
            os.makedirs(outputs, exist_ok=True)
            wr = Writer(outputs, f"search_tree.{re.sub(r'[^A-Za-z0-9]+', '_', term)}", case_dir)
            model = w.search_results_model
            # Hit rows are built on demand (when a file is expanded); list
            # them all so the tree is compared in full.
            for key in list(getattr(w, '_search_file_hits', {})):
                w._search_show_all_hits(key)

            def walk(parent_index, trail):
                for r in range(model.rowCount(parent_index)):
                    cells = [model.index(r, c, parent_index) for c in range(model.columnCount(parent_index))]
                    texts = [model.data(i) for i in cells]
                    roles = {str(k): model.data(cells[0], Qt.ItemDataRole.UserRole + k) for k in range(0, 12)}
                    roles = {k: v for k, v in roles.items() if v is not None}
                    wr.add({"trail": trail, "texts": texts, "roles": roles})
                    walk(cells[0], trail + [texts[0]])
            from PySide6.QtCore import QModelIndex
            walk(QModelIndex(), [])
            stages[f"search:{term}"]["tree"] = wr.close()
    w._stop_all_workers() if hasattr(w, "_stop_all_workers") else None
    import resource
    result["peak_rss_gb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2)
    result["messages_shown"] = shown
    with open(result_path, "w") as f:
        json.dump(result, f, indent=1)


# ── headless stages ──────────────────────────────────────────────────────────

def _open_case(zip_path, case_dir):
    _paths()
    from app import zip_cd_cache as zc
    from app.adapters.ffs import FfsAdapter
    from app.ffs_metadata import load_snapshot_from_case
    infos = zc.load(zip_path, case_dir)
    snap = load_snapshot_from_case(case_dir)
    if infos is None or snap is None:
        raise SystemExit(f"case not ready: {case_dir}")
    view = zc.CachedZipView(zip_path, infos)
    adapter = FfsAdapter.detect(view, frozenset(i.filename for i in infos))
    return zc, infos, snap, view, adapter


def _parsers(zip_path, case_dir, out_dir, result_path):
    zc, infos, snap, view, adapter = _open_case(zip_path, case_dir)
    from app.artifact_runner import load_artifacts, run_artifact, take_open_notes
    from app.db_utils import load_guid_bundle_map
    platform = "android" if adapter.is_android(snap["folder_map"]) else "ios"
    db = sqlite3.connect(os.path.join(case_dir, "casecache.db"))
    try:
        g2b = load_guid_bundle_map(db)
    finally:
        db.close()
    modules, load_errors = load_artifacts(platform)
    timings, digests = {}, {}
    rows_dir = os.path.join(out_dir, "reports")
    os.makedirs(rows_dir, exist_ok=True)
    for name, mod in modules:
        t = time.monotonic()
        try:
            rows, err = run_artifact(name, mod, zip_path, adapter, case_dir, zip_obj=view, guid_to_bundle=g2b)
        except Exception as exc:          # a crash is a result to record, not to hide
            rows, err = [], f"crashed: {type(exc).__name__}: {exc}"
        notes = take_open_notes()
        timings[name] = round(time.monotonic() - t, 3)
        wr = Writer(rows_dir, name, case_dir)
        for r in rows:
            wr.add(r)
        digests[name] = {**wr.close(), "error": _norm(err, case_dir), "notes": _norm(notes, case_dir)}
    with open(result_path, "w") as f:
        json.dump({"platform": platform, "seconds": timings, "reports": digests,
                   "load_errors": _norm(load_errors, case_dir)}, f, indent=1)


def _dump(zip_path, case_dir, out_dir, result_path):
    zc, infos, snap, view, adapter = _open_case(zip_path, case_dir)
    digests = {}
    offsets = zc.compute_data_offsets(zip_path, infos)
    wr = Writer(out_dir, "file_index", case_dir)
    for i in infos:
        wr.add({"name": i.filename, "size": i.file_size, "csize": i.compress_size, "crc": i.CRC,
                "method": i.compress_type, "dos_time": list(i.date_time), "data_offset": offsets.get(i.filename),
                "extra_sha256": hashlib.sha256(i.extra or b"").hexdigest()})
    digests["file_index"] = wr.close()
    wr = Writer(out_dir, "ui_metadata", case_dir)
    for path, meta in snap["ui_metadata"].items():
        wr.add({"ui_path": path, "meta": meta})
    digests["ui_metadata"] = wr.close()
    wr = Writer(out_dir, "folder_map", case_dir)
    for folder, children in snap["folder_map"].items():
        wr.add({"folder": folder, "children": sorted(map(str, children)) if isinstance(children, (list, set, tuple)) else children})
    digests["folder_map"] = wr.close()
    wr = Writer(out_dir, "zip_ui_paths", case_dir)
    for p in snap.get("zip_ui_paths") or []:
        wr.add(p)
    digests["zip_ui_paths"] = wr.close()
    wr = Writer(out_dir, "adapter", case_dir)
    wr.add({"snapshot_adapter": snap["adapter"], "detected": [adapter.format, getattr(adapter, "user_prefix", None)]})
    digests["adapter"] = wr.close()
    for db_name in ("casecache.db", "caseresults.db"):
        path = os.path.join(case_dir, db_name)
        if not os.path.exists(path):
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for t in tables:
                cur = con.execute(f'SELECT * FROM "{t}"')
                cols = [d[0] for d in cur.description]
                wr = Writer(out_dir, f"{db_name[:-3]}.{t}", case_dir)
                for row in cur:
                    wr.add(dict(zip(cols, row)))
                digests[f"{db_name[:-3]}.{t}"] = wr.close()
        finally:
            con.close()
    wr = Writer(out_dir, "case_files", case_dir)
    for base, _dirs, files in os.walk(case_dir):
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, case_dir)
            if rel.split(os.sep)[0] in ("casecache.db", "caseresults.db") or rel.endswith((".db-wal", ".db-shm", ".db-journal")):
                continue
            h = hashlib.sha256()
            if rel.endswith(".mhtml"):
                # The Chrome cache reports write a random MIME boundary into
                # each page (see volatile.json); hashed with it blanked out.
                with open(p, "rb") as f:
                    h.update(_MHTML_BOUNDARY.sub(b"MultipartBoundary--<RANDOM>", f.read()))
            else:
                with open(p, "rb") as f:
                    for block in iter(lambda: f.read(8 << 20), b""):
                        h.update(block)
            wr.add({"path": rel, "size": os.path.getsize(p), "sha256": h.hexdigest()})
    digests["case_files"] = wr.close()
    with open(result_path, "w") as f:
        json.dump(digests, f, indent=1)


# ── orchestration ────────────────────────────────────────────────────────────

def _child(args):
    cmd = [sys.executable, os.path.abspath(__file__), *args]
    t = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=STAGE_TIMEOUT_S)
    return proc.returncode, round(time.monotonic() - t, 1), (proc.stderr or "")[-3000:]


def run(out, run_id, labels):
    for label in labels:
        zip_path = ARCHIVES[label]
        base = os.path.join(out, label, run_id)
        if os.path.exists(base):
            raise SystemExit(f"{base} exists; pick a new --run")
        case_dir = os.path.join(base, "case")
        outputs = os.path.join(base, "outputs")
        os.makedirs(case_dir)
        os.makedirs(outputs)
        summary = {"archive": label, "zip": zip_path, "zip_bytes": os.path.getsize(zip_path),
                   "python": sys.version.split()[0], "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "git": subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                                         text=True).stdout.strip(),
                   "steps": {}}
        for step, args in (("open_cold", ["_gui", zip_path, case_dir, "open", os.path.join(base, "open_cold.json")]),
                           ("session", ["_gui", zip_path, case_dir, "session", os.path.join(base, "session.json")]),
                           ("parsers", ["_parsers", zip_path, case_dir, outputs, os.path.join(base, "parsers.json")]),
                           ("dump", ["_dump", zip_path, case_dir, outputs, os.path.join(base, "dump.json")])):
            rc, secs, err = _child(args)
            summary["steps"][step] = {"rc": rc, "wall_s": secs}
            if rc != 0:
                summary["steps"][step]["stderr_tail"] = err
            print(f"{label} {run_id} {step}: rc={rc} {secs}s", flush=True)
            with open(os.path.join(base, "summary.json"), "w") as f:
                json.dump(summary, f, indent=1)


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return None


_STEPS = ("open_cold", "session", "parsers", "dump")


def _run_problem(out, label, run_id):
    """Why *run_id* of *label* can't be compared, or None: the run is
    missing, one of its steps failed or never ran, or a GUI step exited
    normally but a stage in it did not finish (e.g. Process Case or a search
    hit its time limit: "ok": false in open_cold.json / session.json). Its
    outputs would be partial, and comparing them could look like a real
    difference, or like none."""
    base = os.path.join(out, label, run_id)
    if not os.path.isdir(base):
        return "no such run"
    steps = (_load(os.path.join(base, "summary.json")) or {}).get("steps", {})
    bad = [f"{s} {'did not run' if s not in steps else 'rc=' + str(steps[s]['rc'])}"
           for s in _STEPS if steps.get(s, {}).get("rc") != 0]
    for step in ("open_cold", "session"):
        if steps.get(step, {}).get("rc") != 0:
            continue
        stages = (_load(os.path.join(base, f"{step}.json")) or {}).get("stages")
        if stages is None:
            bad.append(f"{step} recorded no stages")
            continue
        bad += [f"{step}: {name} did not finish" for name, v in stages.items() if not v.get("ok")]
    return ", ".join(bad) or None


def compare(out, a, b, show=5) -> int:
    """Print the differences between runs *a* and *b* on every archive that
    has either. Returns 0 when every such archive was compared and is
    identical, 1 when anything differs, 2 when an archive could not be
    compared (a run missing or incomplete) or no archive has either run;
    nothing is reported identical that was not actually compared."""
    volatile = _load(os.path.join(out, "volatile.json")) or {}
    any_diff = False
    compared, skipped = 0, []
    for label in sorted(os.listdir(out)):
        if not (os.path.isdir(os.path.join(out, label, a)) or os.path.isdir(os.path.join(out, label, b))):
            continue
        problems = [f"{r}: {p}" for r, p in ((a, _run_problem(out, label, a)), (b, _run_problem(out, label, b))) if p]
        if problems:
            print(f"== {label}\n  NOT COMPARED ({'; '.join(problems)})")
            skipped.append(label)
            continue
        compared += 1
        da, db_ = os.path.join(out, label, a, "outputs"), os.path.join(out, label, b, "outputs")
        names = sorted({f for f in os.listdir(da) if f.endswith(".gz")} | {f for f in os.listdir(db_) if f.endswith(".gz")}
                       | {"reports/" + f for f in os.listdir(os.path.join(da, "reports"))}
                       | {"reports/" + f for f in os.listdir(os.path.join(db_, "reports"))})
        print(f"== {label}")
        for n in names:
            key = n[:-len(".jsonl.gz")]
            skip = volatile.get(key, {}).get("fields", [])
            la, lb = _read(os.path.join(da, n), skip), _read(os.path.join(db_, n), skip)
            if la == lb:
                continue
            any_diff = True
            sa, sb = set(la), set(lb)
            print(f"  {key}: {len(la)} vs {len(lb)} rows, {len(sa - sb)} only in {a}, {len(sb - sa)} only in {b}"
                  + (f" (ignoring {skip})" if skip else ""))
            for line in sorted(sa - sb)[:show]:
                print(f"    - {line[:300]}")
            for line in sorted(sb - sa)[:show]:
                print(f"    + {line[:300]}")
        for step in ("open_cold", "session", "parsers"):
            ta, tb = _load(os.path.join(out, label, a, f"{step}.json")), _load(os.path.join(out, label, b, f"{step}.json"))
            if ta and tb:
                print(f"  timing {step}: {_timing(ta)}  ->  {_timing(tb)}")
    if skipped or not compared:
        print(f"INCOMPLETE: {compared} archive(s) compared"
              + (f", not compared: {', '.join(skipped)}" if skipped else f"; no archive in {out} has run {a} or {b}")
              + (" (the compared ones differ)" if any_diff else ""))
        return 2
    print(f"{'DIFFERENT' if any_diff else 'IDENTICAL'} ({compared} archive(s) compared)")
    return 1 if any_diff else 0


def _read(path, skip):
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    if not skip:
        return lines
    out = []
    for line in lines:
        obj = json.loads(line)
        if isinstance(obj, dict):
            for k in skip:
                obj.pop(k, None)
        out.append(_key(obj))
    return sorted(out)


def _timing(t):
    if "stages" in t:
        return ", ".join(f"{k} {v.get('seconds')}s" for k, v in t["stages"].items())
    if "seconds" in t:
        return f"{sum(t['seconds'].values()):.1f}s over {len(t['seconds'])} reports"
    return ""


def main():
    if len(sys.argv) > 1 and sys.argv[1].startswith("_"):
        stage = sys.argv[1]
        if stage == "_gui":
            _gui(*sys.argv[2:6])
        elif stage == "_parsers":
            _parsers(*sys.argv[2:6])
        elif stage == "_dump":
            _dump(*sys.argv[2:6])
        return
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--run", required=True)
    r.add_argument("--archive", action="append", choices=sorted(ARCHIVES), help="default: all six")
    c = sub.add_parser("compare")
    c.add_argument("out")
    c.add_argument("a")
    c.add_argument("b")
    args = ap.parse_args()
    if args.cmd == "run":
        run(os.path.expanduser(args.out), args.run, args.archive or list(ARCHIVES))
    else:
        sys.exit(compare(os.path.expanduser(args.out), args.a, args.b))


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
