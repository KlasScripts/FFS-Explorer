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

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QRadioButton, QVBoxLayout,
)

import device_timezone as _device_timezone
from db_utils import _open_results_db, load_case_setting, save_case_setting
from dialog_helpers import button_row, note_label, WARNING_STYLE

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


class TimestampDisplayMixin:
    """Mixed into FastZipBrowser. Owns: the shared mode banner above every
    tab, the per-case UTC/handset/acquisition/manual mode choice and its
    dialog, and format_ts — the single entry point every other view/mixin
    calls to display an evidence timestamp per the case's active setting.
    """

    def _setup_timestamp_banner(self, layout) -> None:
        """Build the shared timestamp-mode banner and add it to layout —
        one indicator above every tab (File Browser, Media, Search,
        Artifacts) rather than a per-tab label or per-column header suffix,
        since the case's UTC/handset/acquisition/manual setting applies to
        every timestamp shown anywhere. This is the only place it's shown
        — the window title stays plain, so the mode isn't stated twice
        (see _refresh_timestamp_mode_indicator)."""
        self._timestamp_mode_banner = QLabel()
        self._timestamp_mode_banner.setStyleSheet(
            "color: #e07b00; font-weight: bold; font-size: 12px; padding: 2px 6px;")
        self._timestamp_mode_banner.setVisible(False)
        layout.addWidget(self._timestamp_mode_banner)

    def format_ts(self, ts):
        if not ts: return "---"
        mode = getattr(self, '_timestamp_display_mode', 'utc')
        if mode == 'handset' and getattr(self, '_handset_zone_name', ''):
            return _format_ts_named_zone(ts, self._handset_zone_name)
        if mode == 'acquisition' and getattr(self, '_acquisition_zone_name', ''):
            return _format_ts_named_zone(ts, self._acquisition_zone_name)
        if mode == 'manual' and getattr(self, '_manual_zone_name', ''):
            return _format_ts_named_zone(ts, self._manual_zone_name)
        return _format_ts_cached(ts)

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
        return "UTC"

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
                    handset_zone = _device_timezone.detect_handset_zone(
                        zip_path, adapter, case_dir) or ''
                    acquisition = _device_timezone.detect_acquisition_offset(zip_path)
                    save_case_setting(db, 'handset_timezone_name', handset_zone)
                    save_case_setting(db, 'acquisition_offset_hours',
                                      '' if acquisition is None else str(acquisition[0]))
                    save_case_setting(db, 'acquisition_dt',
                                      '' if acquisition is None else acquisition[1].isoformat())
                else:
                    handset_zone = zone_setting
                acquisition_zone = load_case_setting(db, 'acquisition_timezone_name', '') or ''
                manual_zone = load_case_setting(db, 'manual_timezone_name', '') or ''
                mode = load_case_setting(db, 'timestamp_display_mode', 'utc') or 'utc'
        except Exception:
            pass
        return handset_zone, acquisition_zone, manual_zone, mode, is_first_load

    def _timestamp_display_dialog(self):
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
        all). Reachable from the Tools menu at any time; also shown
        proactively the first time a case is opened (see
        _start_case_meta_load)."""
        case_dir = self._case_dir
        if not case_dir:
            return
        try:
            with closing(_open_results_db(case_dir)) as db:
                offset_str = load_case_setting(db, 'acquisition_offset_hours', '') or ''
                acq_str    = load_case_setting(db, 'acquisition_dt', '') or ''
        except Exception:
            offset_str, acq_str = '', ''

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
                f"Handset local — {self._handset_zone_name} (detected from device)")
            group.addButton(handset_radio)
            v.addWidget(handset_radio)

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
                "Acquisition computer —", acquisition_combo,
                f"(the .ufd acquisition report — not this computer, and not "
                f"necessarily the examiner reviewing this case now — recorded "
                f"UTC{'+' if offset_hours >= 0 else ''}{offset_hours:g}:00 at "
                f"acquisition time; {len(acquisition_candidates)} zone(s) share "
                f"that offset — pick the correct one if you know it, otherwise "
                f"the pre-selected guess is only a starting suggestion)",
                'acquisition')

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

        {'utc': utc_radio, 'handset': handset_radio,
         'acquisition': acquisition_radio,
         'manual': manual_radio}.get(current_mode, utc_radio).setChecked(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep)
        v.addWidget(note_label(
            "⚠ Detected/guessed zones are a snapshot at the moment of "
            "seizure only. If the device changed timezone earlier in its "
            "history (travel, DST, a manual change), older evidence will "
            "display in the wrong zone under either non-UTC option. The "
            "\"Acquisition computer\" option is not read from the device at "
            "all, and is NOT this computer or whoever is reviewing the case "
            "right now — it's a guess from the workstation that originally "
            "ran the acquisition, and several real-world zones can share the "
            "same offset, so confirm or correct it above rather than "
            "trusting the pre-selected default. Always verify against UTC "
            "before citing a time in a report.",
            style=WARNING_STYLE))

        btns, _cancel_btn, _ok_btn = button_row(dlg)
        v.addLayout(btns)

        if not dlg.exec():
            return

        if handset_radio and handset_radio.isChecked():
            mode = 'handset'
        elif acquisition_radio and acquisition_radio.isChecked():
            mode = 'acquisition'
        elif manual_radio.isChecked():
            mode = 'manual'
        else:
            mode = 'utc'
        chosen_acquisition_zone = (acquisition_combo.currentData()
                                   if mode == 'acquisition' and acquisition_combo else
                                   self._acquisition_zone_name)
        chosen_manual_zone = (manual_combo.currentData() if mode == 'manual' else
                              self._manual_zone_name)

        self._timestamp_display_mode = mode
        self._acquisition_zone_name = chosen_acquisition_zone or ''
        self._manual_zone_name = chosen_manual_zone or ''
        try:
            with closing(_open_results_db(case_dir)) as db:
                save_case_setting(db, 'timestamp_display_mode', mode)
                save_case_setting(db, 'acquisition_timezone_name', self._acquisition_zone_name)
                save_case_setting(db, 'manual_timezone_name', self._manual_zone_name)
        except Exception:
            pass
        self._refresh_timestamp_mode_indicator()

        if self._view_is_recursive:
            self._rebuild_file_view_from_checked(preserve_filter=True)
        else:
            self._refresh_folder_view(preserve_filter=True)
