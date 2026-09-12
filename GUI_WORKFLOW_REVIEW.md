# GUI workflow review — Validation, Triage, New app, Searching, Reviewing, Bookmarking, Reporting

Scope: not "does the feature exist" but "what does an examiner actually click
through, today, to do this — and is it intuitive and fast." Every claim below
is traced through the real code (file:function cited), not reconstructed from
CLAUDE.md's own descriptions of intent. Where CRUSH_REVIEW.md already covers
directly overlapping ground (global search, the Apps-node triage view), this
builds on that rather than repeating it. Nothing in ios-ffs-browser was
changed to produce this — review only.

## Validation

**Current state.** A parser's validation status lives at exactly one place:
expand the Apps tree → expand that ONE parser's own group → click its
"Validation" leaf (`artifact_viewer.py:1713`, `_art_show_validation` at
line 3094). That opens a plain text page showing either "No validation
baseline recorded for this parser yet" with a "Record This Case as
Validation Baseline" button, or (if one exists) a diff between the baseline
and this case's current schema/folder structure
(`parser_validation.diff_snapshot`/`render_diff_text`). Recording one is a
single confirm-dialog action (`_on_art_record_validation_baseline`,
line 3127).

**Friction points.** Validation status is invisible everywhere else —
grepped the whole file for every other place "validation" is mentioned
(lines 1555-1594, 3044-3155): it is ONLY the one tree leaf. The Apps table
(`_APPS_COLUMNS`, line 3443) has no "Validated" column. A report's own
header, when you're actually reading its rows, says nothing about whether
what you're looking at has ever been checked against a real device. An
examiner has to already remember, per parser, to go check — there is no
passive signal anywhere that "12 of 20 parsers you're relying on have never
been validated on any real device."

**Is it intuitive/fast?** The action itself (recording/viewing a baseline)
is fine once you're on the page — one button, clear confirm dialog, honest
warning text about not doing this against real casework. But discoverability
is poor: nothing surfaces validation status until you go looking for it
per-parser, and nothing tells you it's worth looking at all.

**Concrete next step(s).** Add a "Validated" column (or a small colored dot)
to the Apps table (`_populate_apps_table`, `_APPS_COLUMNS`) sourced from
`validation_store.get(key)` per row — this data already exists and is
already keyed exactly the way the Apps table needs it
(`f'{platform}:{script_name}'`). Zero new computation, just a lookup already
being done per-parser generalized to a table-wide pass.

## Triage

**Current state.** The Apps tree node (`_art_show_apps`, line 3659) loads
`app_intelligence.scan_apps()` into a flat table with a real "Score" column
(`_APPS_COLUMNS`, line 3443: App/Bundle ID/Shared Data Folder/Data
Folder/Plugin(s)/Total Size/Media Files/four timestamp columns/**Score**/Has
Parser/Category). Clicking a column header sorts it (list-mode sort, already
fixed per CLAUDE.md's own history) — so sorting by Score descending to find
the highest-interest unparsed apps genuinely works.

**Friction points, confirmed directly, not assumed.** The table's own column
list does NOT include Evidence Databases, Webview Storage, Hidden Vault
Storage, or Encryption Caveat at all — confirmed by reading `_APPS_COLUMNS`
verbatim above; these fields are computed by `app_intelligence.scan_apps()`
(same function) but are only returned via the MCP `list_apps` tool, per a
direct prior design decision ("the rest can be kept for AI only" — see
CLAUDE.md). This means: an examiner without AI access enabled (or who
hasn't configured an external MCP client — the embedded server has no
built-in chat UI of its own, see "New app not supported" below) sees a bare
NUMBER in the Score column with **no way in the GUI to see why it's high**.
The exact evidence that produced the score — "found `db_sqlite`, 9.4MB,
ranked #1 of 34 candidates" — exists, was computed, and is simply not shown.
Getting from "this app scored an 8" to "here's the actual file to open"
requires either (a) enabling AI access and using an external MCP client, or
(b) manually re-doing `app_intelligence`'s own work by hand in the File
Browser. Separately confirmed: selecting/clicking an Apps-table row does
NOTHING — `_on_art_report_row_selected` (line 1825) resolves hex/attachment
preview off `self._art_current_mod`, which `_populate_apps_table` (line
3626) explicitly sets to `None` for this table, so there is no
Record/Attachment content, no jump-to-File-Browser action, nothing. The
Data Folder/Shared Data Folder columns are plain text paths with no
click-to-navigate at all.

**Is it intuitive/fast?** The ranking itself is genuinely good — sortable,
real signal, matches what CRUSH_REVIEW.md already noted has no real analog
in Crush. But it's a dead end for a non-AI examiner: you can find the
needle, but the tool then hands you nothing to actually go pick it up with.
That's the opposite of "fast" for the exact moment triage is supposed to
save the most time.

**Concrete next step(s).** Two independent, additive fixes, neither touching
the AI-access consent boundary: (1) a "Why" column or an expandable detail
(even a tooltip) on the Score column surfacing the top evidence-database
candidate inline — the data is already computed, this is display-only; (2)
double-click on an Apps-table row jumps the File Browser tab to that app's
Data Folder ui_path (already resolved and shown as a column value) — a real,
bounded navigation action, the single highest-value "make triage fast"
fix in this list.

## New app not supported

**Current state.** Nothing in the plain GUI is aware of "this app has no
parser" as a distinct state to help with. An examiner manually switches to
the File Browser tab, navigates the folder tree to the app's container
(found via the File Browser's own path or, per Triage above, NOT via a
click-through from the Apps table), opens `.db` files one at a time in the
Database tab (`sqlite_viewer.py`, general — works on ANY SQLite file,
confirmed this is one of only two general structured viewers the whole tool
has, see CRUSH_REVIEW.md's own finding on this) and reads raw tables by eye.

**Friction points.** `list_apps`/`get_app_data_locations`/
`build_artifact_parser`/`list_evidence_candidates` (`mcp_server.py`) are ALL
exclusively MCP tools — confirmed via `_toggle_mcp_server`
(`ffs-explorer.py:6190`) — gated behind an explicit Preferences toggle
AND requiring a separately-configured external MCP client (Claude Desktop
or similar) to actually call them; the embedded server has no built-in
chat panel of its own inside ios-ffs-browser. A plain examiner who hasn't
set that up, or who has AI access declined by policy, has literally zero
of this tooling available — not a degraded version, none at all. They are
doing exactly the same manual, unguided folder-and-database exploration
Crush's own general-purpose approach requires — except without any of
Crush's general viewers for LevelDB/ABX/Protobuf/binary-plist content they
might find along the way (per CRUSH_REVIEW.md's own finding: only SQLite
and SEGB are general viewers here).

**Is it intuitive/fast?** No. This is the single least-guided workflow in
the whole tool for exactly the situation (a genuinely new, uncovered app)
where guidance would matter most. An examiner with no AI access is
strictly worse off here than a Crush user would be, since Crush's own
general viewers at least render whatever binary format they find, even
with zero app-specific knowledge.

**Concrete next step(s).** This is the one item in this whole review that's
a real design decision, not a quick fix: either (a) surface a
non-AI-gated, read-only slice of `app_intelligence`'s own output for
exactly this case (the Triage fix above already helps directly), or (b)
build a minimal built-in prompt/wizard reachable from the plain GUI that
walks the same steps `build_artifact_parser`'s MCP prompt already defines,
without requiring an external AI client at all. Don't start either without
deciding which — they're different scopes.

## Searching

**Current state.** Keyword Search (`keyword_search.py`) is a real,
scope-selectable search — "All Files"/"App Data"/saved bookmark groups
(`_refresh_search_scope_combo`, line 652) — running against raw archive
bytes (`KeywordSearchWorker`, `_build_zip_entries`) with results cached per
(term, scope) in the case DB. This is genuinely close to a real
"search everything" tool for raw files.

**Friction points.** Confirmed by reading the whole file: this searches raw
FILES ONLY. It has no path into the already-parsed Artifact Report tables
at all — those have their own, separate, PER-REPORT-ONLY text filter
(`_setup_report_filter_ui`'s filter box, covered under Reviewing below),
which only searches within whichever ONE report is currently open. There is
no single search box that covers both raw files and every parsed report at
once. An examiner looking for "wickr" has to run Keyword Search for the raw
hits, then separately open and filter each relevant report by hand,
one at a time, with no indication which reports are even worth checking.

**Is it intuitive/fast?** The raw-file search itself is solid and already
scope-aware. But "search everything in this case" is not actually one
action — it's two structurally different tools an examiner has to already
know exist and remember to use both.

**Concrete next step(s).** A shared search backend that also queries every
already-populated `artifact_<name>` table (a straightforward SQL `LIKE`
sweep across `caseresults.db`'s own tables, which already exist) and folds
hits into the same results view, tagged by which report they came from —
doesn't require touching the raw-file search path at all, purely additive.

## Reviewing artifacts

**Current state.** This is the tool's real center of gravity: open a
report, browse the Report table (Columns dialog for show/hide/reorder,
Core/All presets), select a row to sync the shared bottom Hex/Text panel
via the Record/Attachment toggle (`_on_art_report_row_selected`, line
1825), double-click a `media_fields` cell to open a persistent media
viewer that follows row selection. This machinery (verified extensively
earlier this session) genuinely works, is well-cited, and is arguably this
project's single strongest feature relative to Crush (which has no
per-app-report concept at all).

**Friction points.** Confirmed by grep: there is NO cross-report
navigation of any kind. `_art_show_report` (line 2238 etc.) is only ever
invoked with a fixed `script_name` from a tree click — nothing lets an
examiner select a WhatsApp message from one contact and jump to that same
contact's other conversations in a different app, or from a Chrome history
row to a related Google Messages conversation, without manually leaving
the report, going back to the tree, and re-navigating (or running a fresh
Keyword Search for the contact's own identifier). Every report is an
island once you're inside it.

**Is it intuitive/fast?** Within one report, yes — genuinely good, this is
where the project's own care is most visible. Across reports, no — every
"is this the same person/conversation elsewhere" question requires
manually restarting from the tree or from Search, every time.

**Concrete next step(s).** Lower priority than the others here since it's
a bigger design question (what counts as "the same identity" across
different apps' own ID schemes is genuinely non-trivial), but worth
naming: even a narrow version — a right-click "Search for this value" on
any cell that runs Keyword Search pre-filled with that cell's text — would
close most of the practical gap cheaply, without solving cross-app
identity resolution in general.

## Bookmarking

**Current state.** Bookmarks (`db_utils.py:913-969`,
`ffs-explorer.py:6828-7082`) are FILE-level: named groups (default
"Evidence"/"Interesting") each holding a list of raw archive paths, created
via `_add_to_bookmark_group(self, paths, group_id)` — a right-click action
in the File Browser (and, per `keyword_search.py:674/799`, also reachable
from Search results). Separately, `research_store.py` provides its own,
different mechanism: notes/status keyed by app/stream identity, driving
row coloring in the Artifact Viewer.

**Friction points.** Confirmed directly: bookmarking operates on file
PATHS, never on an individual Artifact Report ROW. There is no "flag this
one WhatsApp message as significant" action anywhere — the most granular,
most forensically specific unit this tool produces (a cited, record_source
-backed report row) has no bookmark/flag of its own at all. An examiner
wanting to mark a specific message has to bookmark the WHOLE underlying
database file instead, losing which row within it actually mattered.
Bookmarks and research notes are also confirmed as two genuinely separate
systems with different scopes (file-collection vs. app-identity status
color) — an examiner has to know both exist and remember which one to
check for what.

**Is it intuitive/fast?** File-level bookmarking itself is straightforward.
But it's the wrong granularity for the tool's own best feature (per-row
citation) — an examiner doing careful review has no way to flag "this
exact row" and come back to it later without re-finding it by hand.

**Concrete next step(s).** Extend bookmark entries to optionally carry a
report row's own real citation (script_name + rowid/record_source key,
already computed for the Hex panel jump) alongside the existing file-path
form — additive to the existing schema, not a replacement, and reuses
data this project already has on hand at the moment a row is selected.

## Reporting

**Current state.** Checked exhaustively for any way to get a parsed
Artifact Report's OWN rows out of the tool: no CSV/export action anywhere
in `artifact_viewer.py` (grepped for "export"/"Export" — the only hits are
the unrelated "Exported Files" tree leaf, which shows the parser's own RAW
SOURCE files it read, not its output rows), no context menu on the report
table (`customContextMenuRequested` — zero hits), no clipboard/copy action.
The only export capability anywhere in the whole tool is the File Browser's
own `ExportProgressDialog` (`ffs-explorer.py:1855`), which extracts raw
archive files/folders to disk — nothing about a parsed report's structured
output.

**Friction points.** This is close to a hard stop, not friction. An
examiner who has built up a well-cited, reviewed Artifact Report has no
built-in way to hand that table to a supervisor, opposing counsel, or a
case file — screenshotting or manually re-typing rows is the only option
today. The AI Summary panel's own narrative text is presumably
selectable/copyable (a plain `QTextBrowser`), but that's a generated
narrative, not the underlying structured, cited data, and per
CRUSH_REVIEW.md's own separate finding, nothing here produces a hash
manifest for chain-of-custody either.

**Is it intuitive/fast?** There's nothing to be intuitive about — the
capability doesn't exist. This is the single largest concrete gap found in
this entire review, bigger in practical terms than any UX friction in the
other six workflows, since it means the tool's real output currently has
no path to leaving the tool at all.

**Concrete next step(s).** A straightforward "Export Report to CSV" action
on the Report table (the underlying `ArtifactTableModel` already has
uniform row/column access for both DB-mode and list-mode reports — this is
close to a plain iteration + `csv.writer`, no new data layer needed) is the
single highest-value, lowest-effort fix in this whole review. A hash
manifest on export (per CRUSH_REVIEW.md) is a natural, cheap addition once
export exists at all.

## Window layout: detachable panels for multi-monitor use

A direct design question came up after this review: should the four center
tabs (File Browser/Media Browser/Keyword Search/Artifact Viewer) move to a
Cellebrite-style single main table, driven by a left-hand category tree,
instead of Qt tabs?

**Considered and rejected — a full move to one unified table.** Cellebrite's
single-table model works because it normalizes every category into one
generic row shape. This project's own design deliberately goes the other
way — a WhatsApp report's columns are meaningfully different from a Chrome
History report's, and that specificity (correctly-typed, per-app columns,
`record_source` citations, `media_fields`) is exactly what this tool has
that a generic table doesn't; forcing everything into one shape would cost
that. It also doesn't literally fit: Media Browser is a thumbnail *grid*,
not a table at all — even Cellebrite keeps Gallery view as its own separate
mode rather than folding it into the same table. Most importantly, none of
the three real gaps found earlier in this review (cross-report navigation,
the search/report split, bookmark granularity) are caused by tabs being
tabs — a unified table would still have every one of them. The one part of
the Cellebrite model actually worth having — one detail view that reacts to
whatever's currently selected, regardless of which mode you're in — is
already built here: the shared bottom Hex/Text panel already does exactly
that across all four tabs. Not recommended.

**Worth doing: let a tab detach into its own window (Qt dock widgets).**
A genuinely different, much smaller, well-understood problem from the
above — Qt's dock-widget system exists specifically to let a panel live in
a tab strip, or get dragged out into its own floating/dockable window, on
the same monitor or a second one. This wouldn't touch any tab's own
internal content, model, or the existing per-tab state-preservation work
(`_on_center_tab_changed`, `_resync_*_preview` — see "Per-tab state on
switching" in CLAUDE.md) at all — only the *container* the four tabs sit
inside would change, from a plain `QTabWidget` to a dock-widget-based
layout where each tab can optionally be un-docked. This directly serves a
real, named, currently-impossible workflow: an examiner cross-checking a
File Browser listing against Keyword Search hits side by side, on two
monitors — today only one tab's content can ever be visible at a time,
regardless of how many screens are attached, no matter how good any one
tab's own UI is. Recommended as the one layout-level change worth pursuing
from this discussion, in preference to any Cellebrite-style rewrite.

## Consolidated TODO (prioritized)

- [ ] **Add CSV export for Artifact Report tables** (`app/artifact_viewer.py`,
  the Report table view). Highest priority — not a UX polish item, a
  currently-total absence of any way to get parsed output out of the tool.
  Cheap: `ArtifactTableModel` already has uniform row access.
- [ ] **Surface WHY an app scored high, inline, in the Apps table**
  (`_populate_apps_table`/`_APPS_COLUMNS`, `app/artifact_viewer.py`) — the
  evidence-database/webview-storage detail is already computed by
  `app_intelligence.scan_apps()`, just not shown outside the AI-only MCP
  path. Cheap, display-only.
- [ ] **Double-click an Apps-table row jumps the File Browser to that
  app's Data Folder** (`_on_art_report_row_selected`/`_populate_apps_table`,
  `app/artifact_viewer.py`) — closes the "found it, now what" gap Triage
  currently has. Bounded, moderate effort (needs a File-Browser-tab-switch
  + tree-select call from the Artifact Viewer).
- [ ] **Add a "Validated" indicator column to the Apps table**
  (`_populate_apps_table`, `app/artifact_viewer.py`), sourced from
  `validation_store.get()` per row — cheap, the lookup already exists, just
  not run table-wide.
- [ ] **Extend the search backend to also query already-populated
  `artifact_<name>` tables**, not just raw files (`app/keyword_search.py`
  + a new small cross-table query helper) — closes the "two separate
  search tools" gap. Moderate effort.
- [ ] **Let a bookmark entry optionally carry a report row's own citation**
  (record_source key), not just a file path (`app/db_utils.py`'s bookmark
  schema + `ffs-explorer.py`'s bookmark-adding actions) — additive schema
  change, moderate effort given the UI for adding one needs to live inside
  the Artifact Viewer too, not just the File Browser.
- [ ] **Decide, deliberately, how (or whether) to help a "new app, no
  parser" examiner without AI access** — genuinely a design decision, not
  a quick fix; don't start building either a non-AI triage surface or a
  built-in wizard without settling which first.
- [ ] **Lower priority: a right-click "Search for this value" on any
  Report table cell**, pre-filling Keyword Search — closes most of the
  practical cross-report navigation gap cheaply without attempting general
  cross-app identity resolution.
- [ ] **Lower priority, depends on export existing first: a hash manifest
  on report export** (cross-reference: CRUSH_REVIEW.md's own finding on
  this) — trivial once CSV export exists, not worth building standalone
  first.
- [ ] **Let the four center tabs detach into their own windows** (Qt dock
  widgets, replacing the plain `QTabWidget` container in `ffs-explorer.py`)
  — genuine multi-monitor support (e.g. File Browser and Keyword Search
  open side by side on two screens). Doesn't touch any tab's own internal
  content or the existing per-tab state-preservation work at all, only the
  container. Bigger than the other UI items above (a real layout change,
  not a widget addition) but bounded and additive — not a rewrite.
