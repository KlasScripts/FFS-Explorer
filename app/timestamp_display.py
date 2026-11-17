"""timestamp_display.py — evidence timestamp display: UTC/handset/acquisition/
manual mode, the shared mode banner, and the Timestamp Display dialog.

Extracted from ffs-explorer.py (2026-08-19) as part of splitting that file's
remaining tangled concerns into per-concern modules, matching the mixin
pattern already used by HexViewerMixin/MediaViewerMixin/etc. Moved as a
unit with no behaviour change — see VERIFICATION_STATUS.md for the
before/after regression check this was verified against.

See CLAUDE.md's Conventions section for the full evidence-vs-tool-
provenance timestamp policy this implements. Tool-provenance formatting
(_format_tool_ts_local) is a DIFFERENT concern and stays in ffs-explorer.py
— it's about when the examiner did something with this app, never
evidence, so it doesn't belong in this module.
"""

import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QRadioButton, QVBoxLayout,
)
from PySide6.QtCore import Qt

import archive_offset as _archive_offset
import device_timezone as _device_timezone
from db_utils import _open_results_db, load_case_setting, save_case_setting
from dialog_helpers import note_label, WARNING_STYLE

# ── Module-level formatting helpers (pure — no FastZipBrowser state) ───────────

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


@lru_cache(maxsize=4096)
def _date_prefix(days: int) -> str:
    """'YYYY-MM-DD' for a day number since the epoch (cached — few unique days)."""
    return (_EPOCH_UTC + timedelta(days=days)).strftime('%Y-%m-%d')


@lru_cache(maxsize=1 << 16)
def _format_ts_cached(ts) -> str:
    """Format an epoch timestamp (s or APFS ns) as a UTC display string.

    strftime per row dominated recursive-view row building, so the date part
    is cached per day and the time of day is computed arithmetically —
    ~5x faster on archives with diverse timestamps, identical output.
    """
    try:
        if ts > 1e10:
            ts = ts / 1e9   # APFS stores timestamps as nanoseconds since epoch
        days, rem = divmod(int(ts // 1), 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return f"{_date_prefix(days)} {h:02d}:{m:02d}:{s:02d} UTC"
    except (ValueError, OSError, OverflowError):
        return "---"


# ── Opt-in device-local / acquisition-local / manual evidence display ─────────
#
# UTC (_format_ts_cached above) is the default and the only mode with no
# caveat needed — see CLAUDE.md's Conventions section. The other three are
# explicit, user-chosen, per-case exceptions applied only to the main file
# browser's Modified/Created columns (see FastZipBrowser.format_ts).
#
# All operate on the SPECIFIC timestamp being formatted, never on
# datetime.now() / whoever is CURRENTLY reviewing the case's own clock —
# confirmed deliberately: reviewing a case in a different season/year (or a
# different timezone entirely — e.g. reviewing a US-Eastern acquisition
# from the UK) must not change how old evidence displays.

@lru_cache(maxsize=1 << 14)
def _format_ts_named_zone(ts, zone_name: str) -> str:
    """Evidence timestamp in a named IANA zone, DST resolved for THIS
    timestamp's own date (a January value correctly gets EST, a July one
    EDT, regardless of when the case is being reviewed) — used for all
    three non-UTC display modes, all of which resolve to a real named zone
    (handset: read from the device; acquisition: a user-confirmed guess
    from the acquisition workstation's recorded offset — see
    device_timezone.guess_acquisition_zones; manual: picked outright), not
    a flat non-DST-aware offset.

    Only the resolved abbreviation (EST/EDT/...) goes in the per-cell
    string, matching how UTC mode's own cells just say "UTC" — the zone
    NAME and which source it came from is constant for the whole column
    while a mode is active, so it's stated once in the shared mode banner
    instead of repeated on every row. The abbreviation is the part that
    actually varies row-to-row (DST), so it's the part worth keeping inline.

    Both detected/guessed sources are snapshots at/near the moment of
    seizure only — see the timestamp-display dialog's warning text for why
    that may not apply to data from earlier in the device's history.
    """
    try:
        secs = ts / 1e9 if ts > 1e10 else ts   # APFS nanoseconds, same check as _format_ts_cached
        dt = datetime.fromtimestamp(secs, tz=timezone.utc).astimezone(ZoneInfo(zone_name))
        return f"{dt:%Y-%m-%d %H:%M:%S %Z}"
    except Exception:
        return _format_ts_cached(ts)


# Bumped when handset-zone detection learns a new source, so a case saved
# under the old rule is detected once more ('2': Android persist.sys.timezone).
HANDSET_ZONE_RULE = '2'


class TimestampDisplayMixin:
    """Mixed into FastZipBrowser. Owns: the shared mode banner above every
    tab, the per-case UTC/handset/acquisition/manual mode choice and its
    dialog, and format_ts — the single entry point every other view/mixin
    calls to display an evidence timestamp per the case's active setting.
    """

    def _setup_timestamp_banner(self) -> QLabel:
        """Build the shared timestamp-mode banner widget — one indicator
        above every tab (File Browser, Media, Search, Artifacts) rather
        than a per-tab label or per-column header suffix, since the
        case's UTC/handset/acquisition/manual setting applies to every
        timestamp shown anywhere. This is the only place it's shown — the
        window title stays plain, so the mode isn't stated twice (see
        _refresh_timestamp_mode_indicator).

        Deliberately does NOT add itself to a layout (unlike its own
        pre-2026-09-13 version) — per direct instruction not to waste
        vertical screen space, the caller now places this on the SAME
        row as the header-scan banner instead of stacking them; see
        ffs-explorer.py's own __init__ for that shared row."""
        self._timestamp_mode_banner = QLabel()
        self._timestamp_mode_banner.setStyleSheet(
            "color: #e07b00; font-weight: bold; font-size: 12px; padding: 2px 6px;")
        self._timestamp_mode_banner.setVisible(False)
        return self._timestamp_mode_banner

    def format_ts(self, ts):
        if not ts: return "---"
        mode = getattr(self, '_timestamp_display_mode', 'utc')
        if mode == 'handset' and getattr(self, '_handset_zone_name', ''):
            return _format_ts_named_zone(ts, self._handset_zone_name)
        if mode == 'acquisition' and getattr(self, '_acquisition_zone_name', ''):
            return _format_ts_named_zone(ts, self._acquisition_zone_name)
        if mode == 'manual' and getattr(self, '_manual_zone_name', ''):
            return _format_ts_named_zone(ts, self._manual_zone_name)
        if mode == 'archive':
            choice = getattr(self, '_archive_zone_choice', '')
            if choice == 'fixed' and self._archive_fixed_offset() is not None:
                return _archive_offset.format_fixed(ts, self._archive_fixed_offset())
            if choice and choice != 'fixed':
                return _format_ts_named_zone(ts, choice)
        return _format_ts_cached(ts)

    def _archive_fixed_offset(self):
        det = getattr(self, '_archive_offset_detect', None) or {}
        return det.get('offset_s') if det.get('status') == 'fixed' else None

    def _timestamp_mode_text(self) -> str:
        """One-line description of the active timestamp-display mode —
        shared by the banner and the window title so they never drift."""
        mode = getattr(self, '_timestamp_display_mode', 'utc')
        if mode == 'handset' and getattr(self, '_handset_zone_name', ''):
            return f"Handset local time ({self._handset_zone_name})"
        if mode == 'acquisition' and getattr(self, '_acquisition_zone_name', ''):
            return f"Acquisition PC local time ({self._acquisition_zone_name})"
        if mode == 'manual' and getattr(self, '_manual_zone_name', ''):
            return f"Manually selected time ({self._manual_zone_name})"
        if mode == 'archive':
            choice = getattr(self, '_archive_zone_choice', '')
            fixed = self._archive_fixed_offset()
            if choice == 'fixed' and fixed is not None:
                return f"Archive zip DOS−UT, extraction tool's zone ({_archive_offset.offset_text(fixed)}, fixed)"
            if choice and choice != 'fixed':
                return f"Archive zip DOS−UT, extraction tool's zone ({choice}, consistent with the zip)"
        return "UTC"

    def _after_timestamp_mode_change(self) -> None:
        """Redraw every place that shows evidence times after the setting
        changed: banner, Database tab, File Browser, the open artifact
        report and the Timeline. Report and Timeline cells are converted
        as they are drawn, so without this the cells on screen kept the
        old zone until something repainted them."""
        self._refresh_timestamp_mode_indicator()
        refresh_db_times = getattr(self, '_sql_refresh_times', None)
        if callable(refresh_db_times):
            refresh_db_times()
        art_model = getattr(self, '_art_table_model', None)
        if art_model is not None:
            art_model.layoutChanged.emit()
        refresh_timeline = getattr(self, '_timeline_refresh_times', None)
        if callable(refresh_timeline):
            refresh_timeline()
        if self._view_is_recursive:
            self._rebuild_file_view_from_checked(preserve_filter=True)
        else:
            self._refresh_folder_view(preserve_filter=True)

    def _refresh_timestamp_mode_indicator(self):
        """Updates the one shared timestamp-mode banner (above every tab —
        File Browser, Media, Search, Artifacts), so the active
        UTC/handset/acquisition/manual setting is stated once, prominently,
        rather than repeated per-column or per-view. Replaces the earlier
        per-tab label and per-column artifact-table header suffix (both
        removed — a single always-visible indicator is clearer than
        several easy-to-miss ones). The window title deliberately stays
        plain — this banner is the only place the mode is shown, not
        stated twice. Hidden until a case is actually open, since no mode
        is meaningful before then."""
        if not self._case_dir:
            self._timestamp_mode_banner.setVisible(False)
            return
        self._timestamp_mode_banner.setText(f"Time setting: {self._timestamp_mode_text()}")
        self._timestamp_mode_banner.setVisible(True)

    def _load_or_detect_timezone_settings(self, zip_path: str, adapter, case_dir: str):
        """Return (handset_zone_name, acquisition_zone_name, manual_zone_name,
        display_mode, is_first_load) for this case, from case_settings if already
        detected, otherwise running the CHEAP detection once (device zone;
        raw acquisition-workstation offset + timestamp) and persisting it
        so it never silently changes between sessions of the same case.

        is_first_load is True only the very first time this case is ever
        opened — the caller uses it to proactively show the timestamp-
        display dialog once, right when the case is created, rather than
        leaving the option undiscovered behind a menu item. The menu item
        (see _timestamp_display_dialog) stays available to revisit the
        choice at any later time too.

        Deliberately does NOT run guess_acquisition_zones() here — matching
        ~600 IANA zones against the acquisition offset happens lazily, only
        when the dialog is actually shown (first load, or a later manual
        open) — not on every ordinary case load. acquisition_zone_name is
        '' until the user has confirmed one via that dialog — until then,
        'acquisition' mode has nothing to display and format_ts falls back
        to UTC.

        handset_zone_name is '' if not detected (Android, or the file was
        missing/unparseable). manual_zone_name is '' until the user has
        confirmed one via the "Manually selected zone" option in the
        dialog — it is never auto-detected the way handset/acquisition
        are, only ever a deliberate pick (see detect_system_zone's
        docstring for why this project won't guess it). display_mode
        defaults to 'utc'."""
        handset_zone, acquisition_zone, manual_zone, mode, is_first_load = '', '', '', 'utc', False
        try:
            with closing(_open_results_db(case_dir)) as db:
                zone_setting = load_case_setting(db, 'handset_timezone_name')
                if zone_setting is None:
                    # Never detected for this case — run the cheap part once now.
                    is_first_load = True
                    handset_zone = self._detect_and_save_handset_zone(db, zip_path, adapter, case_dir)
                    acquisition = _device_timezone.detect_acquisition_offset(zip_path)
                    save_case_setting(db, 'acquisition_offset_hours',
                                      '' if acquisition is None else str(acquisition[0]))
                    save_case_setting(db, 'acquisition_dt',
                                      '' if acquisition is None else acquisition[1].isoformat())
                elif (load_case_setting(db, 'handset_timezone_rule', '') or '') != HANDSET_ZONE_RULE:
                    # Detected by an older rule (iOS only until 2026-11-16):
                    # an Android case saved '' then. Detect once more; a
                    # zone the examiner may already be displaying in is
                    # only replaced if the new rule finds one.
                    handset_zone = (self._detect_and_save_handset_zone(db, zip_path, adapter, case_dir)
                                    or zone_setting)
                else:
                    handset_zone = zone_setting
                acquisition_zone = load_case_setting(db, 'acquisition_timezone_name', '') or ''
                manual_zone = load_case_setting(db, 'manual_timezone_name', '') or ''
                mode = load_case_setting(db, 'timestamp_display_mode', 'utc') or 'utc'
                archive = self._load_or_detect_archive_offset(db, zip_path, case_dir)
        except Exception:
            archive = {'detect': None, 'choice': ''}
        return handset_zone, acquisition_zone, manual_zone, mode, is_first_load, archive

    @staticmethod
    def _detect_and_save_handset_zone(db, zip_path: str, adapter, case_dir: str) -> str:
        """The phone's own zone setting (iOS db/timezone/localtime, Android
        persist.sys.timezone) saved with the file it came from, so the
        dialog, Device Info and the Timezone report name the same source."""
        info = _device_timezone.detect_handset_zone_info(zip_path, adapter, case_dir) or {}
        zone = info.get('zone') or ''
        source = f"{info['source']} ({info['setting']})" if zone else ''
        save_case_setting(db, 'handset_timezone_name', zone)
        save_case_setting(db, 'handset_timezone_source', source)
        save_case_setting(db, 'handset_timezone_rule', HANDSET_ZONE_RULE)
        return zone

    @staticmethod
    def _load_or_detect_archive_offset(db, zip_path: str, case_dir: str) -> dict:
        """{'detect': archive_offset.detect() result or None, 'choice':
        'fixed', a zone name, or ''}. Detected once per case from the
        cached zip index and stored (case_settings 'archive_offset'), so it
        never changes between sessions; not stored if the index could not
        be read, so it is tried again next time."""
        raw = load_case_setting(db, 'archive_offset', None)
        det = None
        if raw:
            try:
                det = json.loads(raw)
            except ValueError:
                det = None
        if det is None:
            try:
                det = _archive_offset.detect_from_archive(zip_path, case_dir)
                det['rule'] = 1
                save_case_setting(db, 'archive_offset', json.dumps(det))
            except Exception:
                det = None
        return {'detect': det,
                'choice': load_case_setting(db, 'archive_zone_choice', '') or ''}

    def _timestamp_display_dialog(self, is_first_load: bool = False):
        """Per-case choice of how evidence timestamps are displayed in the
        main file browser's Modified/Created columns: UTC (default), the
        device's own detected zone, a zone guessed from the acquisition
        WORKSTATION's recorded offset and confirmed/corrected by the user
        (the machine that ran UFED and wrote the .ufd — not the machine
        currently reviewing this case, which can be a different person, a
        different computer, and a different timezone entirely), or a
        manually-picked zone from the full IANA list — always available,
        unlike the two detected/guessed options, which need a handset file
        (iOS only) or a .ufd (Cellebrite only; GrayKey extractions have
        neither, so manual selection can be the only non-UTC option at
        all). Reachable from the Tools menu at any time (is_first_load
        stays False there — see the explicit lambda at that connection
        site, which avoids Qt's own triggered(bool) signal ambiguity);
        also shown proactively the first time a case is opened (see
        _start_case_meta_load / _show_timestamp_dialog_when_ready, which
        passes is_first_load=True).

        is_first_load=True additionally hands off to Process Case once
        this dialog is accepted — added 2026-09-26, direct request
        replacing the earlier CaseSettingsDialog-triggered automatic
        background header scan ("instead of doing the head[er] scan in
        the background can we instead just[,] after the user has
        selected how they want to deal with the time and date[,] the ok
        should be tied to opening process case and the hash check
        should be checked[,] the tier one should be ticked, zip and
        embedded media"). See FastZipBrowser._open_process_dialog_for_new_case."""
        case_dir = self._case_dir
        if not case_dir:
            return
        try:
            with closing(_open_results_db(case_dir)) as db:
                offset_str = load_case_setting(db, 'acquisition_offset_hours', '') or ''
                acq_str    = load_case_setting(db, 'acquisition_dt', '') or ''
                handset_source = load_case_setting(db, 'handset_timezone_source', '') or ''
        except Exception:
            offset_str, acq_str, handset_source = '', '', ''

        acquisition_candidates: list[str] = []
        acquisition_guess = None
        offset_hours = None
        acq_dt = None
        if offset_str and acq_str:
            try:
                offset_hours = float(offset_str)
                acq_dt = datetime.fromisoformat(acq_str)
                acquisition_guess, acquisition_candidates = _device_timezone.guess_acquisition_zones(
                    offset_hours, acq_dt)
            except Exception:
                pass

        dlg = QDialog(self)
        dlg.setWindowTitle("Timestamp Display")
        dlg.setModal(True)
        dlg.setMinimumWidth(600)
        # No Cancel, no [x], no Escape — direct request, 2026-09-25: "can
        # you remove the cancel for the time and date? they need to
        # choose." UTC is always a safe, harmless default to just accept
        # (nothing here can be "wrong"), so this only ever costs an extra
        # OK click on a dialog the examiner would otherwise dismiss
        # without deciding anything. The close-button removal
        # (WindowCloseButtonHint) isn't guaranteed on every platform's own
        # native title bar (confirmed a real, known Qt limitation on
        # macOS in particular), so closeEvent/keyPressEvent are ALSO
        # overridden below as the actual enforcement — belt and suspenders,
        # not relying on the hint alone.
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)
        def _dlg_close_event(event):
            event.ignore()
        dlg.closeEvent = _dlg_close_event
        def _dlg_key_press_event(event, _orig=QDialog.keyPressEvent):
            if event.key() == Qt.Key.Key_Escape:
                event.ignore()
                return
            _orig(dlg, event)
        dlg.keyPressEvent = _dlg_key_press_event
        v = QVBoxLayout(dlg)

        v.addWidget(note_label(
            "Choose how evidence timestamps are shown in the file browser's "
            "Modified/Created columns. This applies to this case only.",
            style=""))

        group = QButtonGroup(dlg)
        current_mode = getattr(self, '_timestamp_display_mode', 'utc')

        utc_radio = QRadioButton("UTC (default)")
        group.addButton(utc_radio)
        v.addWidget(utc_radio)

        handset_radio = None
        if self._handset_zone_name:
            handset_radio = QRadioButton(
                f"Handset local — {self._handset_zone_name} (from phone)")
            group.addButton(handset_radio)
            v.addWidget(handset_radio)
            v.addWidget(note_label(
                "(The phone's own time zone setting at extraction"
                + (f", read from {handset_source}" if handset_source else "")
                + ". It does not say which zone the phone was in on earlier dates.)"))

        def _zone_offset_label(zone_name: str) -> str:
            # All candidates share the same offset at acq_dt by construction
            # (that's the match criterion) — computed per-zone anyway via
            # ZoneInfo rather than reusing the raw offset_hours float, so
            # the label always reflects exactly what conversion would use.
            try:
                off = ZoneInfo(zone_name).utcoffset(acq_dt)
                total_min = int(off.total_seconds() // 60)
                sign = '+' if total_min >= 0 else '-'
                hh, mm = divmod(abs(total_min), 60)
                return f"UTC{sign}{hh:02d}:{mm:02d}"
            except Exception:
                return ""

        def _add_zone_row(label_text: str, combo: QComboBox, note_text: str, mode_key: str):
            # The "radio + combo + note + wire enabled-state to the radio"
            # shape acquisition and manual both need — each combo's own
            # ITEM population stays at its call site below, since that part
            # genuinely differs (offset-labeled real candidates vs. a
            # placeholder + convenience entry + full sorted list).
            row = QHBoxLayout()
            radio = QRadioButton(label_text)
            group.addButton(radio)
            row.addWidget(radio)
            row.addWidget(combo, 1)
            v.addLayout(row)
            v.addWidget(note_label(note_text))
            combo.setEnabled(current_mode == mode_key)
            radio.toggled.connect(combo.setEnabled)
            return radio

        acquisition_radio = None
        acquisition_combo = None
        if acquisition_candidates:
            acquisition_combo = QComboBox()
            for zone in acquisition_candidates:
                offset_label = _zone_offset_label(zone)
                text = f"{zone} ({offset_label})" if offset_label else zone
                acquisition_combo.addItem(text, zone)
            preselect = self._acquisition_zone_name or acquisition_guess
            if preselect and preselect in acquisition_candidates:
                acquisition_combo.setCurrentIndex(acquisition_candidates.index(preselect))
            acquisition_radio = _add_zone_row(
                "Acquisition PC —", acquisition_combo,
                f"(the .ufd acquisition report — not this computer, and not "
                f"necessarily the examiner reviewing this case now — recorded "
                f"UTC{'+' if offset_hours >= 0 else ''}{offset_hours:g}:00 at "
                f"acquisition time; {len(acquisition_candidates)} zone(s) share "
                f"that offset — pick the correct one if you know it, otherwise "
                f"the pre-selected guess is only a starting suggestion)",
                'acquisition')

        archive_radio = None
        archive_combo = None
        det = getattr(self, '_archive_offset_detect', None) or {}
        if det.get('status') in ('fixed', 'dst'):
            archive_combo = QComboBox()
            fixed = self._archive_fixed_offset()
            if fixed is not None:
                archive_combo.addItem(
                    f"Fixed {_archive_offset.offset_text(fixed)} (from the zip)", 'fixed')
            else:
                archive_combo.addItem("— pick a zone consistent with the zip —", None)
            if det.get('zones'):
                archive_combo.insertSeparator(archive_combo.count())
                for zone in det['zones']:
                    archive_combo.addItem(f"{zone} — consistent with the zip", zone)
            idx = archive_combo.findData(getattr(self, '_archive_zone_choice', '') or None)
            if idx >= 0:
                archive_combo.setCurrentIndex(idx)
            n = det.get('n') or 0
            seen = ', '.join(f"{c:,} at {_archive_offset.offset_text(o)}"
                             for o, c in (det.get('bins') or [])[:2]) if n else ''
            pattern = ("one fixed offset" if fixed is not None else
                       "two offsets an hour apart (daylight saving), so no single "
                       "fixed offset fits; pick a zone")
            row = QHBoxLayout()
            archive_radio = QRadioButton("Archive (zip DOS vs UT) —")
            group.addButton(archive_radio)
            row.addWidget(archive_radio)
            row.addWidget(archive_combo, 1)
            v.addLayout(row)
            v.addWidget(note_label(
                "(Inferred from how this zip records each file's time twice: UT, UTC, "
                "and DOS, a local time the extraction software wrote at download in "
                "the zone it was set to, usually the download computer's. It is not "
                "the phone's setting and says nothing about where the phone was when "
                "a file was made. Prefer UTC for reporting unless you need local. "
                f"Sampled {n:,} files: {seen}: {pattern}. "
                f"{len(det.get('zones') or [])} zones give the same offset as the zip for "
                f"at least {_archive_offset.ZONE_MATCH:.0%} of the sampled files, each at "
                "its own date; none is picked for you.)"))
            archive_combo.setEnabled(current_mode == 'archive')
            archive_radio.toggled.connect(archive_combo.setEnabled)

        # Manual zone — always offered, unlike the one above (which needs a
        # .ufd to guess from and simply doesn't exist for e.g. a GrayKey
        # Android extraction, where this may be the ONLY way to get a
        # non-UTC display at all). Deliberately starts on the placeholder,
        # never pre-selected to a real zone, UNLESS the examiner already
        # confirmed one in an earlier session of this same case
        # (self._manual_zone_name) — that's a returning choice, not a
        # silent default.
        manual_combo = QComboBox()
        manual_combo.addItem("— select a zone —", None)
        system_zone = _device_timezone.detect_system_zone()
        if system_zone:
            manual_combo.addItem(f"This computer's current zone — {system_zone}", system_zone)
            manual_combo.insertSeparator(manual_combo.count())
        for zone in sorted(available_timezones()):
            manual_combo.addItem(zone, zone)
        if self._manual_zone_name:
            idx = manual_combo.findData(self._manual_zone_name)
            if idx >= 0:
                manual_combo.setCurrentIndex(idx)
        manual_radio = _add_zone_row(
            "Manually selected zone —", manual_combo,
            "(pick any IANA zone yourself — not detected or guessed from "
            "anything. This is the only non-UTC option available when "
            "there's no .ufd to guess an acquisition offset from (e.g. "
            "GrayKey) and no detected handset zone. \"This computer's "
            "current zone\", if listed, is only a convenience starting "
            "point — it's whichever machine is running this app right "
            "now, not the device's zone and not the acquisition "
            "workstation's.)",
            'manual')

        ({'utc': utc_radio, 'handset': handset_radio,
          'acquisition': acquisition_radio, 'archive': archive_radio,
          'manual': manual_radio}.get(current_mode) or utc_radio).setChecked(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep)
        v.addWidget(note_label(
            "⚠ Detected/guessed zones are a snapshot at the moment of "
            "seizure only. If the device changed timezone earlier in its "
            "history (travel, DST, a manual change), older evidence will "
            "display in the wrong zone under any non-UTC option. Only "
            "\"Handset local\" is read from the phone. \"Acquisition PC\" "
            "and \"Archive (zip DOS vs UT)\" describe the extraction: the "
            "workstation that ran the acquisition and the zone the extraction "
            "software wrote the zip's local times in. Neither is this "
            "computer or whoever is reviewing the case now, and several "
            "real-world zones can share one offset, so confirm or correct "
            "them rather than trusting a pre-selected default. Always verify "
            "against UTC before citing a time in a report. The Timezone & "
            "time sources artifact report sets the sources side by side.",
            style=WARNING_STYLE))

        # OK only -- no Cancel (see the dlg construction above for why).
        btns = QHBoxLayout()
        btns.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(dlg.accept)
        btns.addWidget(ok_btn)
        v.addLayout(btns)

        dlg.exec()

        if handset_radio and handset_radio.isChecked():
            mode = 'handset'
        elif acquisition_radio and acquisition_radio.isChecked():
            mode = 'acquisition'
        elif archive_radio and archive_radio.isChecked() and archive_combo.currentData():
            mode = 'archive'
        elif manual_radio.isChecked():
            mode = 'manual'
        else:
            mode = 'utc'
        chosen_acquisition_zone = (acquisition_combo.currentData()
                                   if mode == 'acquisition' and acquisition_combo else
                                   self._acquisition_zone_name)
        chosen_manual_zone = (manual_combo.currentData() if mode == 'manual' else
                              self._manual_zone_name)

        if mode == 'archive':
            self._archive_zone_choice = archive_combo.currentData() or ''
        self._timestamp_display_mode = mode
        self._acquisition_zone_name = chosen_acquisition_zone or ''
        self._manual_zone_name = chosen_manual_zone or ''
        try:
            with closing(_open_results_db(case_dir)) as db:
                save_case_setting(db, 'timestamp_display_mode', mode)
                save_case_setting(db, 'archive_zone_choice',
                                  getattr(self, '_archive_zone_choice', '') or '')
                save_case_setting(db, 'acquisition_timezone_name', self._acquisition_zone_name)
                save_case_setting(db, 'manual_timezone_name', self._manual_zone_name)
        except Exception:
            pass
        self._after_timestamp_mode_change()

        if is_first_load:
            self._open_process_dialog_for_new_case()
