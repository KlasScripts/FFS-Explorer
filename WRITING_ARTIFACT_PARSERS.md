# Writing Your Own Artifact Parser

This is a how-to for examiners who want FFS Explorer to parse an app it
doesn't support yet. It assumes no Python experience beyond reading the
examples below — the format is deliberately small.

## What a parser actually is

A parser is one `.py` file dropped into `artifacts/ios/` or
`artifacts/android/`. It declares which files it needs from the app's own
folder, and a `run()` function that turns those files into rows. FFS
Explorer extracts the files, calls `run()`, and shows the result as a
Report in the Artifacts tab — nothing more.

A parser never touches the archive itself (read-only evidence, always) and
never talks to the network. It only reads the file paths it's handed and
returns `list[dict]`. It also doesn't depend on any of this project's
`app/`-code UI/viewer machinery — the one deliberate exception is a small,
stable set of generic utilities in `app/artifact_runner.py` meant to be
imported directly (see "Reusable helpers" below), since that file always
ships with FFS Explorer itself. That's what "self-contained" means here:
you could copy the one `.py` file to someone else's copy of this app and
it would work.

## The fast way: ask the app to draft one

FFS Explorer has a built-in AI-access feature (Tools → Enable AI Access)
that lets an AI assistant read the case's file structure and database
schemas — never raw evidence content unless you separately opt in — and
draft a parser for you. With raw-content access enabled, ask it to
`build_artifact_parser` for the app you want; it will:

1. Find the app's container and its database file(s).
2. Read the schema and sample real rows (never a full dump).
3. Draft a `.py` file matching the format below, as a code block for you
   to review.

**It never installs or runs the draft itself.** You save the file into
`artifacts/ios/` or `artifacts/android/` yourself, after reading it. Treat
an AI-drafted parser exactly like one written by a colleague — verify it
against real data before trusting its output (see Validating below).

The rest of this document explains the format directly, for writing one by
hand or reviewing a drafted one.

## The format

```python
name = "My App Messages"

# Android: the app's private data folder.
app_path = "data/data/com.example.myapp"

# iOS third-party apps sharing data via an App Group use app_group instead
# — the container is a random GUID per install, resolved automatically.
# Use app_path OR app_group, never both.
# app_group = "group.com.example.myapp.shared"

files = {
    "main_db": "databases/messages.db",
}
optional_files = {
    "main_db_wal": "databases/messages.db-wal",
    "main_db_shm": "databases/messages.db-shm",
}

def run(paths):
    from artifact_runner import open_db_readonly
    conn = open_db_readonly(paths["main_db"])

    rows = conn.execute("SELECT id, sender, body, sent_at FROM messages").fetchall()
    conn.close()

    return [
        {
            "sender": r["sender"],
            "message": r["body"],
            "timestamp": r["sent_at"],
            "raw_message_id": r["id"],
        }
        for r in rows
    ]
```

That's a complete, working parser. Everything else below is optional
polish that makes the report better — add it when it applies, skip what
doesn't.

### `files` / `optional_files`

`files` are required — if one is missing from the archive, the parser
doesn't run and FFS Explorer says why. `optional_files` are extracted when
present but never block the run.

You don't need to list a database's `-wal` or `-journal`: since
2026-11-16 the runner extracts both beside every SQLite file it extracts
(`_extract_db_sidecars`), from the same place in the archive as the
database, so a WAL's newest rows and an unfinished change are never missed
because a parser forgot them. Declaring them anyway is harmless. The `-shm`
is not needed (SQLite rebuilds it from the WAL); list it only if your parser
reads it as evidence.

### `run(paths)`

`paths` is `{key: extracted_file_path}` for everything you declared, plus
one reserved key you didn't: `paths['_app_base_ui_path']` — the app's
container path inside the archive, for when a database column stores a
path to some *other* file (an attachment, a photo) that you want to
reference rather than extract yourself. See `media_fields` below.

`run()` must return `list[dict]`. Every dict in the list should have the
same keys — different key sets between rows means the Report table gets
ragged columns. Always raw values, never a Python string you built with an
f-string — that's what `timestamp_fields` and `media_fields` (below) are
for; the display layer formats them at view time, so the same raw data
still works correctly however the examiner has timestamps configured.

### `description` (recommended)

A one-paragraph string explaining where the data comes from, what's
verified vs. inferred, and any known gap. Shows in the Report's "Notes"
page. Write it as if a different examiner has to trust your report without
reading your code — because that's exactly the situation.

### Notes page: `<script>.notes.md` (recommended for any report with interpreted values)

Next to the parser, `artifacts/<platform>/<script>.notes.md` is shown, rendered,
as the report's **Report Notes/Warning** page instead of `description` (owner
request, 2026-10-24). The app puts a work-in-progress line at the top of every
Notes page: the report should not be used in court without additional
validation. Do not write that the report is ready for court. Write it for a digital forensics examiner with about a
year's experience: plain sentences, bullets and tables, with no wall of text.
Structure (see `artifacts/ios/whatsapp.notes.md`):

1. **Title and 2–3 sentences**: what the report is, and which columns are
   interpreted (those are the ones that can mislead).
2. **"What could catch you out"**: a table of ⚠️ risk, what it means for the
   examiner, and a link to its section (`[Deleted messages](#deleted-messages)`).
3. **One section per risk or decision** (deleted messages, disappearing
   messages, who "Me" is...): how it works, how it shows in FFS Explorer, and
   a worked example from a test image.
4. **End every section with "#### ✅ Validation"**: published / verified on
   ground truth / observed only / not tested, each with its basis.
5. **Interpreted values get a table**: value, meaning, how the report shows
   it, confidence, basis. A column copied straight from the database needs
   nothing.
6. **Not in this report**, then **Sources**.

Images can sit next to the file and be referenced relatively. Editing the
notes doesn't change the parser's version (only the `.py` is hashed).

### `category` / `app_group_label` (where the report is listed)

Run Artifact Parsers and the Artifact Viewer list reports as Category →
App → Report. Every shipped parser has its place in
`config/artifact_categories.json` (`tests/test_artifact_categories.py`
checks it). A parser may also declare its own, which wins:

```python
category = "Chats & Messaging"   # one of the file's "categories" list
app_group_label = "WhatsApp"     # only if the app has more than one report
```

The categories are: Chats & Messaging, Browsers, Usage & Activity,
Notifications, Media & Files, Device & App Info. An app node is only shown
when that app has more than one report. A parser with no valid category is
listed under "Other (category not set)".

### `timestamp_fields` (recommended if there's a timestamp)

```python
timestamp_fields = {"timestamp": "s"}
```

`{field_name: unit_code}` — unit codes are `"s"` (Unix seconds), `"ms"`
(Unix milliseconds), `"cocoa_s"` (Cocoa/Mac epoch seconds — iOS databases),
`"cocoa_ns"` (Cocoa epoch nanoseconds — used by iOS's `message` table
specifically), or `"webkit_us"` (Chromium/WebKit epoch microseconds —
`base::Time`'s internal representation, used throughout Chrome's own
SQLite stores: History, Web Data, segmentation_platform's `ukm_db`, ...).
**Always pass the raw stored value through unconverted and let the unit
code do the conversion** — do not hand-convert to a different epoch in
`run()` before returning it (e.g. converting webkit-microseconds to
Unix-ms yourself and then declaring `"ms"`). A carved row (a `recovered_records`
report) carries the *raw* column value straight from the table, in its
original epoch, under the same field name a hand-converted live row would
use — if `run()` converts but the carved path can't, the same field name
ends up holding two different units depending on whether the row is live
or recovered, and the declared unit code is only ever correct for one of
them. This was a real, shipped bug in this project's own Chrome parsers,
found via a real recovered row landing off by centuries once formatted.
The Report formats the raw value per the case's UTC /
handset / acquisition / manual timestamp-display setting — never bake a
formatted string into `run()`'s output. **Never use SQLite's `'localtime'`
modifier or a bare `datetime.fromtimestamp(x)`** — both silently convert
using the *analysis machine's* timezone, not the evidence's, and can look
correct by pure coincidence depending on time of year. This has been a
real, shipped bug more than once in this project.


### `timeline_fields` / `timeline_skip` (to put the times on the Timeline tab)

```python
timeline_fields = {
    "event_time": {
        "meaning": "App or notification event recorded by Android Usage Stats",  # required
        "app": "package",            # column holding the app (or "app_name": fixed text)
        "event": "event_type",       # column holding the event (or "event_name": fixed text)
        "detail": ["class_name"],    # columns joined into the one-line Detail
        "caution": "A clock change of more than 2 s moves every earlier time.",
    },
}
timeline_skip = {"expires": "the date the trash is due to be emptied, not an event"}
```

A time column is on the Timeline only when its parser says, in plain words,
what the time means. Write the meaning and the caution from the report's own
notes or warning, never from the column name. The unit comes from
`timestamp_fields`. A time column that is not an event (an expiry, a value
worked out from the clock) goes in `timeline_skip` with the reason. Any time
column in neither is listed in the Timeline's Sources as "meaning not written
yet". Code: `app/timeline.py`; test: `tests/test_timeline.py`.

### `media_fields` (if the app has photos/video/attachments)

```python
media_fields = ["attachment_path"]
```

Names the output field(s) holding a full archive **ui_path** (not a raw
database value, not a filesystem path) to a media file. The Report table
then shows a thumbnail there, with double-click opening it full-size or
playing it. Build the ui_path in `run()`:

**Required whenever the report points at files** (owner, 2026-10-21;
ASSUMPTIONS.md A-41): every parser whose rows name an attachment, photo,
video, voice note or other file in the archive must declare it here. This
is also what fills the File Browser's "Linked Report" column, so an
examiner looking at a media file can see which report row mentions it.
Check the links on a real case before shipping: run the parser, then
confirm in the File Browser that a linked file's "Linked Report" names the
right row.

If `media_fields` points at something you decoded to disk rather than at the
archive file itself, also declare `link_fields = ["<column with the archive
ui_path>"]` (Chrome cache parsers use `raw_ui_path`), or `link_fields = []`
when rows have no per-row archive file. Build any archive path with
`paths['_ui_path_for']('mobile/...')` or from `paths['_app_base_ui_path']`,
never a hard-coded `'mobile/...'`: GrayKey iOS keeps the `private/var/`
prefix.

```python
attachment_path = f"{paths['_app_base_ui_path']}/{row['media_column']}"
```

**Images stored inside a database (BLOB columns).** The embedded-media sweep
shows them as children of the database, named
`<db>-<table>-<column>-<rowid><ext>`. To link a report row to one, put
`artifact_runner.embedded_media_path(paths, file_key, table, column, rowid,
blob)` in a column and list that column in `link_fields` (and
`hidden_fields`: it is plumbing). It uses the sweep's own naming code, so
the two can't drift; the link resolves once the sweep has run on that
database. Examples: `whatsapp.py` (Android, `message_thumbnail`, rowid =
message `_id`), `chrome_favicons.py` (`favicon_bitmaps.image_data`).

```python
from artifact_runner import embedded_media_path
row["thumbnail_link"] = embedded_media_path(paths, "msgstore", "message_thumbnail",
                                            "thumbnail", message_id, blob)
```

**Messaging reports: `chat_groups`.** Declare it and the Artifact Viewer adds a
"Chat Groups (N)" folder under the report: one entry per conversation,
`Name (<messages>)`. Clicking the folder shows a summary table (chat,
participants, messages, incoming, outgoing, media in/out, first message, last
received, last sent; double-click a row to open it); clicking a chat shows the
report limited to that conversation (filters and sorting stay within it).

```python
chat_groups = {"key": "raw_chat_id",          # column identifying the conversation
               "name": ["conversation"],      # first non-empty = remote party / group name
               "participants": ["sender"],    # sender of an incoming message
               "sent": ["Sent"],              # direction value(s) meaning sent by the user
               "system": ["System"],          # direction value(s) of app events (optional)
               "media": ["attachment_path"],  # non-empty on a media message
               "media_prefixes": ["[Media]"], # and/or message-text prefixes
               "message": "message",          # message-text column (default)
               "time": "timestamp",           # unit taken from timestamp_fields
               "members": "chat_members",     # everyone in the chat (see below)
               "type": "chat_type",           # "Group" / "1:1" / "Channel"... per row
               "deleted": {"recovered": ["True"], "deleted_at": "*"},
               "call_pattern": r"^\[Call\]",  # regex on the text; omit if no calls
               "call_field": "event",         # match call_pattern on this column instead
               # deletion / disappearing messages (iOS WhatsApp declares these):
               "sort": "chat_sort", "issued": "chat_issued",   # position + chat counter
               "setting": "disappearing_setting",               # "Off" / "7 days" on change rows
               "expires": "disappears_at",                      # per-message expiry
               "deleted_for_everyone": "deleted_for_everyone"}  # "... (by me)" / "(by them)"
```

Keep the message-text column exactly as the app stored it. A label the
parser writes itself (a call, a system line, "Photo") belongs in its own
column, such as `event`, with `call_field` pointing at it, so a search, a
keyword list or a quote of the text never finds the parser's own words, and
a typed "video call?" is not counted as a call (see `android/whatsapp.py`).

With `sort`/`issued`/`setting`, the summary adds: Issued / Present, Missing
(Gaps), Missing At End, Deletion Windows, Deleted For Everyone (Me/Them),
Disappearing, Set By, Set At, Setting History, Missing Consistent With Timer,
Missing Not Explained. State each field's validation status (published /
observed / not published) in the parser's `description`, as `ios/whatsapp.py`
does.

The summary table's columns: Chat, Type, Participants (the device owner, then
everyone in the chat), Messages, Incoming, Outgoing, Media In, Media Out,
Deleted, Calls (blank when the report holds no calls), Links, Bookmarked (rows
whose linked files are bookmarked), Last 30 Days (before the acquisition time
when known, else the newest message), First Message, Last Received, Last Sent.

**Chat members, not recipients (owner decision, 2026-10-24).** Don't write a
per-message "recipients" column: group membership changes over time and the
app's member table is as at extraction, so it can't say who received a given
message. Write a hidden `chat_members` column instead (the same value on every
row of a chat): `Me (<owner's number>)` first, then the 1:1 partner or every
member from the app's own membership data, or "[group members not recorded]".
Take the owner's identity from the app's database or settings, never guess;
write each person the same way as in `sender`, so the summary lists them once;
say in `description` that membership is as at extraction.

**Files named some other way.** Turn what the database stores into an archive
path, and only link it if `archive_has(paths, ui_path)`:
- an Android device path or `file://` URI: `device_path_to_ui_path(p)`
  (`/storage/emulated/0/...` -> `data/media/0/...`, `/data/user/0/...` ->
  `data/data/...`): `chrome_downloads.py`;
- a MediaStore URI (`content://media/.../<id>`): `resolve_mediastore_uri(paths,
  uri)`, through MediaStore's own `external.db`: `viber.py`;
- a cache keyed by the URL: build the cache file name (e.g. Glide's
  `image_manager_disk_cache/<sha256(url)>.0`): `burner.py`.
A link column can hold several paths, one per line (e.g. every face crop of a
photo in `photos_metadata.py`). Don't guess a mapping you haven't confirmed on
real data: GroupMe's image cache didn't match any URL hash tried, so its URLs
stay unlinked.

**Check what the database column actually points at before assuming it's
a usable local path** — some apps store a remote URL (nothing local to
show), or a `content://` URI (meaningless outside a live device). Both are
real, common cases — WhatsApp, Google Messages and GroupMe all needed
different handling here on this project. And the path segment the app
gives you is sometimes not the *complete* relative path — WhatsApp iOS's
database column is missing a `Message/` folder segment that has to be
added by hand. **Verify the constructed path actually resolves to a real
file in the archive before trusting it** — use `find_paths` (via the MCP
tools, or the file browser's search) on a real example filename first.

### `hidden_fields` (if you added a field only for `record_source` below)

```python
hidden_fields = ["raw_group_member_id"]
```

Names output field(s) to keep out of the Report table entirely — for a
value you need internally (see `record_source` next) but that has no
value as something an examiner would want to see as a column. Don't hide
a field just because it's raw/technical — `raw_message_id` and
`source_table` stay visible even where `record_source` is declared,
because the row's own id and which table it came from are still useful
citation info on their own. Only hide a field that exists *purely* to
make `record_source` work and means nothing on its own (a joined table's
internal rowid, say).

### `core_fields` (recommended — which columns matter most)

```python
core_fields = ["timestamp", "url", "title"]
```

Names the subset of your report's own output fields an examiner most
needs on a first pass — not every field your parser happens to produce,
just the ones that actually answer "what happened." Drives the Report
table's own "Columns" dialog (its own row above the report, above
Filter): it always offers "All" / "None", and — only when this list is
declared — a third "Core" preset that jumps straight to it. Every
individual column is still there as its own checkable, reorderable row
in the dialog's list regardless of whether you declare this;
`core_fields` only adds the one-click shortcut, it never hides anything
by itself the way
`hidden_fields` does. Skip it if every column your parser produces is
about equally important — there's no requirement to declare one.

### `requires_nested_extraction` (if your target file lives inside an embedded archive)

```python
requires_nested_extraction = ["some/backup.zip"]
```

This project deliberately never decompresses or header-scans anything
automatically — the examiner can process everything, do it selectively
via a dialog, or do nothing at all, and each is a valid, supported
choice (see ProcessDialog). That means **you, the parser author, are the
only one who knows** whether the app you're targeting packs its real
data inside a compressed blob rather than as a plain file in the
archive — work that out while exploring the app's real container
(cross-referencing iLEAPP/ALEAPP, checking `list_apps`' new
`embedded_archives` field for that app) and declare it here if so.

Each entry is a subpath relative to the resolved app base (same
glob-capable convention as `files`/`optional_files` — not a `files` key
itself). Before your `files`/`optional_files` are resolved, each
declared archive is extracted (via this project's existing nested-
archive mechanism — the exact same code path the examiner's own manual
"Extract as Nested Archive" uses, so a prior manual extraction is
reused instead of redone, and one triggered here is visible to the
examiner afterward too). The result lands in a reserved
`paths['_nested_archives']` dict — `{ui_path: on-disk extracted zip
path}` — for your own `run()` to open with plain `zipfile` calls and
read whatever it needs; this project doesn't try to make `files`/
`optional_files` transparently reach inside it for you, since the
internal layout of an arbitrary embedded archive isn't something a
generic path-resolution step can guess at. A declared archive that
can't be found or fails to extract does NOT abort your parser — it's
recorded in `paths['_nested_archive_errors']` (`{ui_path: message}`)
instead, so `run()` can decide for itself whether that's fatal or just
means less data this time, the same "missing is fine, you decide"
shape `optional_files` already has.

### `record_source` (if you want the Hex panel's "Record" jump to work)

```python
record_source = {
    "file_key":     "main_db",          # a files/optional_files key
    "table_field":  "source_table",     # output field naming the source table
    "rowid_fields": ["raw_message_id"], # output field holding that table's rowid
}
```

Lets an examiner select a report row and jump the Hex panel straight to
that row's own on-disk database cell — real evidence citation, not just
"trust the report." `rowid_fields` must be a genuine SQLite rowid alias
(an `INTEGER PRIMARY KEY` column) for the named table, not a synthetic id
— check the table's own `CREATE TABLE` statement (via `get_sqlite_schema`
or the Database tab) before declaring this, the same rigor as `media_fields`
above. **A joined report** (most non-trivial ones) usually wants **one
entry per joined table**, as a list:

```python
record_source = [
    {"label": "Message", "file_key": "main_db",
     "table_field": "source_table", "rowid_fields": ["raw_message_id"]},
    {"label": "Chat", "file_key": "main_db",
     "table": "chats", "rowid_fields": ["raw_chat_id"]},   # fixed table name — never varies per row
]
```

With more than one entry, a small picker appears next to the Hex panel's
Record/Attachment toggle so the examiner can choose which joined table's
cell to view. The picker is rebuilt fresh each time a DIFFERENT row is
selected, always defaulting back to that row's own first matching entry —
selecting a join from the dropdown is a deliberate per-row choice, never
sticky onto whatever the next row happens to be.

**If your `run()` merges more than one live SQL query into one report**
(e.g. `chrome_web_history.py`, which reads Chrome's own History AND its
separate segmentation_platform/ukm_db as one table, tagged via a `source`
output field) — each query can have a genuinely different main table plus
joins, so give every entry a `source_match` list naming which `source`
value(s) it applies to:

```python
record_source = [
    {"label": "Query A Main", "file_key": "db_a", "table": "main_table",
     "rowid_fields": ["raw_id"], "source_match": ["Query A"]},
    {"label": "Query A Joined URL", "file_key": "db_a", "table": "urls",
     "rowid_fields": ["raw_url_id"], "source_match": ["Query A"]},
    {"label": "Query B Main", "file_key": "db_b", "table": "urls",
     "rowid_fields": ["raw_url_id"], "source_match": ["Query B"]},
]
```

A row from Query A only ever offers the two Query-A entries (defaulting to
its own main table); a row from Query B only ever offers its own single
entry — never a join that structurally can't apply to it. Omit
`source_match` entirely for the common single-query case (every existing
parser but `chrome_web_history.py`) and every entry applies to every row,
unchanged from before this existed. Real, worked example:
`artifacts/android/chrome_web_history.py`.

Two entries sharing a bare table name across different files (real case
above: Chrome History's own `urls` vs. `ukm_db`'s `urls`) is fine for a
LIVE row (`source_match` already disambiguates it) but needs one more
thing for a CARVED row of that table: in a `recovered_records` report, pin
each table to its file with `file_key` (see below) — the carving pass already tags a recovered row with which file it
came from, which the Hex panel uses to pick the right entry even though
`source_match` itself never runs for a carved row (it already knows its
real source table directly).

The output field the entry points at usually already needs
to exist for something else (a raw id you're already keeping per the rule
above) — but sometimes doesn't, and you'll need to add one more `SELECT`
column purely to make this work (see `artifacts/ios/whatsapp.py`'s group-
member/media-item entries for a real example of exactly that). **Don't
guess this one** — declaring it wrong doesn't fail loudly, it silently
points the examiner at the wrong bytes, which is worse than not having
the feature at all. If you can't confirm a table's rowid is real, leave
that entry out; the report still works, that row's Record mode just says
"not available."

A row a `recovered_records` report carved (below) still has a
real, exact citation too — the carving pass already knows precisely which
bytes it found, in either the main db file or a `-wal` sidecar — so
`record_source` covers both live and recovered rows for free once
declared; nothing extra to write for the recovered case.

**If your parser has no database at all** — it reads its own set of
files directly (a binary/LevelDB/whatever format), often a different
real file per row because you enumerated a whole directory yourself
rather than declaring a fixed `files`/`optional_files` entry (see
`artifacts/android/chrome_sessions.py`/`chrome_app_tabs.py` for real
examples) — `file_key`/`table`/`rowid_fields` don't apply: there's no
SQL row to look up, and no single fixed path to resolve through. Use
`ui_path_field` instead — your own `run()` already knows each row's
real archive ui_path and byte span, so just say which output fields
hold them:

```python
record_source = [
    {"label": "Navigation Entry", "ui_path_field": "raw_ui_path"},
    # optional: "offset_field"/"length_field" default to "raw_offset"/
    # "raw_length" if you use those exact field names, as here
]
hidden_fields = ["raw_ui_path", "raw_offset", "raw_length"]
```

`raw_ui_path` is usually hidden (plumbing, not report content, per
`hidden_fields` above) — set it to the exact archive ui_path of the
specific physical file THIS row came from, `raw_offset`/`raw_length` to
the exact byte span within it. Everything else (`source_match`,
`presence_fields`, more than one entry for a real "join" — e.g. an
outer per-file header plus one specific record decoded from within it,
two different byte ranges of the SAME file) works identically to the
SQL-backed entries above; `presence_fields` falls back to
`[ui_path_field]` itself when you don't declare one, so a row with
nothing at that field (nothing decoded for it) is dropped from the
picker the same way a missed SQL JOIN is.

### Deleted and changed records: their own report, never mixed in

A main report holds live rows only (ASSUMPTIONS.md A-95, 2026-11-21).
`recoverable_tables` (carving a table on every run and appending what it
found to the live rows) is retired: it took 23.9 s of a 74 s run for
500,000 WhatsApp messages, usually to find nothing. Deleted and older
records go in a separate `report_type = "recovered_records"` report per
app, covering its message table and its media/attachment tables (below);
`tests/test_recovered_reports.py` stops a shipped parser declaring
`recoverable_tables` again.

A deleted-records report names its main report with `deleted_of =
"<main script name>"`: the loader gives it the main report's app location
(`app_path` / `app_group`) and every database file its tables name
(`file_key`, from the main report's `files` or `optional_files`), so they
are declared once; anything it declares itself wins (LINE's full-text
index reads a database the main report doesn't). The main report then
shows a line linking to it. Use the main report's `file_key` names.

Identical copies of a record (the same shown values under any row id, as
an app that rewrites a row by delete + insert leaves) are one row: its
`copies` column counts them and the hidden `copy_locations` keeps where
each was found (A-95). Put `copies` in `core_fields` and
`copy_locations` in `hidden_fields`, as the shipped reports do.

For a media or attachment table, declare `orphan_of` to also list its
LIVE rows whose message is gone (status Orphaned): not deleted, so never
recovered, and missing from the main report, which lists a row only with
its message. `{"column": "message_row_id", "in": ["message", "_id"]}`, or
with `"via": [join table, its column for this row, its column for the
message]` when a join table links them (iOS SMS attachments). Compared by
id, never joined. On the Josh Hickman iOS 17 image this found 1 of 28 SMS
attachments.

### `report_type = "recovered_records"`: a deleted / changed records report with no `run()`

For "what was deleted or changed in this app's messages", don't write a
`run()` at all. Declare the database and, per table, the columns that
matter; the shared code (`artifact_runner.recovered_records_report`, using
the one carver, `app/sqlite_recovery.py`, the Database tab uses too) makes
the report. **One table per report** (ASSUMPTIONS.md A-95, owner decision
2026-11-27): an app's message table and its media table each get their own
deleted report, both naming the same main report in `deleted_of`. Examples:
`artifacts/android/whatsapp_deleted.py` and
`artifacts/android/whatsapp_deleted_media.py` (messages and media, linked
by `links`), `artifacts/ios/sms_deleted.py` and
`artifacts/ios/sms_deleted_attachments.py` (linked through a join table),
`artifacts/android/burner_messageentity.py` (a
JSON-blob column split with `json_fields`, a checked caveat with
`field_notes`),
`artifacts/ios/line_deleted_messages.py`.

```python
name = "LINE — Deleted & Changed Messages (iOS)"
description = "..."
category = "Chats & Messaging"
app_group = "group.com.linecorp.line"
files = {"line": "Library/Application Support/PrivateStore/P_*/Messages/Line.sqlite"}

report_type = "recovered_records"
recovered_records = [{
    "file_key": "line", "table": "ZMESSAGE", "label": "Message",
    "fields": {"ZTIMESTAMP": "timestamp", "ZTEXT": "message", "ZSENDER": "sender_id"},
    "version_fields": ["ZTEXT", "ZSENDER"],            # what makes a version matter
    "labels": {"ZCONTENTTYPE": {0: "text", 1: "picture"}},          # optional
    "derived": {"direction": {"from": "ZSENDER", "values": {None: "Outgoing"},
                              "else": "Incoming"}},                  # optional
}]
timestamp_fields = {"timestamp": "ms"}
```

What each row is, and the rules:

- One recovered record exactly as found in one table. **Never joined** to
  other tables: ids stay ids (owner decision 2026-11-17: the recovered
  record is the evidence; looking up names is a separate, later step).
- `status`: **Deleted** (no longer in the table), **Changed** (an earlier
  version of a row that is still there), **Unfinished write / delete**
  (hot journal). `how_found` and `table_certain` say how and how sure.
- **Changed** rows are listed only when a `version_fields` column differs
  from the live row, with the live value beside it as `<name> (now)` and
  `changed_fields` naming what changed. Choose the columns that carry
  meaning: the text, the sender, the chat, the kind, and the app's own
  deleted / edited / type field (WhatsApp marks "This message was deleted"
  by its type, not its text). Leave out delivery and sync columns. Versions
  that differ only in other columns are counted in the run log, never
  silently dropped. No `version_fields`: every version is listed.
- `labels` turn a stored code into a word, shown with the stored value:
  `picture (1)`. `derived` adds a column worked out from one value
  (direction): from fixed values (`"values": {None: "Outgoing"}`), or, when
  the deciding ids differ per phone, from a lookup (`"in": {"table":
  "ZCHATMEMBER", "column": "Z_PK", "where": "ZISLOGGEDINUSER = 1"}, "then":
  "Outgoing", "else": "Incoming"`; no field of that table is added). See
  `artifacts/ios/life360_deleted_messages.py`. **Only declare a label or derived value you checked against
  ground truth, and say in a comment what you checked.**
- `not_found_in` (optional): list a record only when a column's value is
  not the value of another table's column in any live or recovered row,
  e.g. a chat's last-message preview whose text is in no message. A
  comparison of values, not a join.
- `links` (optional): a column saying where the rows of another table
  that this record names were found: `message 5: live`, `message 5:
  recovered (in <its deleted report>)`, `message 5: not found`, or, for a
  one-to-many link, the row ids (`parts with message_id = 100: live row
  67; recovered row 66 (in ...)`). `"via"` goes through a join table, whose
  rows are themselves taken live or recovered (iOS SMS). Ids are compared;
  no value of the other row is shown. `"report"` names the other table's
  deleted report (checked when the reports load). The description must say
  how the link is made and why it can be wrong: whether the ids can be
  handed out again (AUTOINCREMENT, Core Data's Z_PRIMARYKEY counter, or
  neither; check the schema on a test image), that a WAL version and a
  live row with the same id are from different times, that a record found
  by pattern has no row id, and that copies share an id.
- Use the same field names as the app's other reports (`timestamp`,
  `message`, `direction`) so they line up.
- The `-wal` / `-journal` need not be declared (the runner extracts them).
  `record_source` is made for you (the Hex panel jumps to the record's
  exact bytes).

### Reusable helpers

A parser is meant to stay self-contained (see "What a parser actually is"
above) — but a couple of tiny, generic utilities in `app/artifact_runner.py`
are the one deliberate exception, importable directly from `run()`:

```python
from artifact_runner import first_nonempty
attachment_path = first_nonempty(row["full_media_path"], row["thumbnail_path"])
```

`first_nonempty(*values)` returns the first truthy value, or `""` if
they're all empty — for an app that sometimes only has a smaller cached
preview locally instead of the full media file (WhatsApp iOS's
`ZMEDIALOCALPATH`/`ZXMPPTHUMBPATH` is the real example this was built
for). If you find yourself needing the same small building block a second
time, add it to that file's "Parser helpers" section rather than
re-writing it inline — that's what it's there for.

**Always open your evidence sqlite file through `open_db_readonly`, never
a bare `sqlite3.connect()`** — see the example at the top of this
document. A bare connect, even for a read-only `SELECT`, can trigger
SQLite's own checkpoint-on-close behavior and silently delete the file's
own `-wal`/`-shm` sidecars before anything else (a deleted-records
report's carving, or just a re-opened case) gets a chance to read them —
destroying deleted-row history that was recoverable a moment earlier.

```python
from artifact_runner import open_db_readonly
conn = open_db_readonly(paths["main_db"])   # already row_factory=sqlite3.Row
```

If your parser needs a second file via `ATTACH DATABASE`, attach it as its
own read-only URI too — a read-only main connection does NOT make an
attached database read-only on its own (confirmed by direct testing):

```python
conn.execute("ATTACH DATABASE ? AS other", (f'file:{paths["other_db"]}?mode=ro',))
```

**If your app stores data in LevelDB** (Chrome's own Local Storage,
IndexedDB, and Session Storage all do — see
`artifacts/android/chrome_local_storage.py` for a real, complete
example), use `open_leveldb` instead of hand-rolling the extract-then-
open dance yourself:

```python
from artifact_runner import open_leveldb

db = open_leveldb(paths, "app_chrome/Default/Local Storage/leveldb")
if db is None:
    return []   # directory doesn't exist in this archive at all

try:
    for rec in db.iterate_records_raw():
        ...   # rec.user_key, rec.value, rec.state (Live/Deleted/Unknown), rec.seq
finally:
    db.close()
```

LevelDB never overwrites a key in place — an update or delete just
writes a new record at a higher sequence number, so `iterate_records_raw()`
hands back every version, live and superseded/deleted, not just the
current one (see `rec.state`/`rec.seq` above). `open_leveldb`'s first
argument is the directory's own path relative to the app's container
base (no leading/trailing slash); the extracted files land under this
parser's own `artifact_parser_files/` folder, same as any other
extracted evidence, and a file already extracted from a prior run is
reused rather than re-copied.

## Validating what you wrote

Every parser here was built against one specific test image (a known
device + app version) and its output cross-checked against documented
ground truth before being trusted. Do the same for yours:

1. **Run it against a real archive and read the actual rows** — not just
   "the code compiles." Check a join didn't silently drop rows (compare
   `SELECT COUNT(*)` on the raw table against your output count), and that
   an unresolved foreign key shows up as `"[no chat record — raw
   chat_id=...]"` rather than a blank field or a dropped row.
2. **Record a validation baseline** (Artifacts tab → your parser →
   Validation → "Record This Case as Validation Baseline") **only** against
   a case you know is the GTD/known-version image you built the parser
   against — never against real casework. This snapshots the database
   schema and the app folder's structure, so a *future* run against a
   different app version will show you exactly what changed (a new table,
   a renamed column, a new database file), instead of the parser silently
   going stale.
3. Grep your own diff for `fromtimestamp(` and `'localtime'` before
   calling it done — see the timestamp warning above.

## Where the real detail lives

This document is the quick-start. `CLAUDE.md`'s Conventions section is the
canonical reference for every convention mentioned above (and a few more:
`recovery_field_notes`, `warning`, the raw-id-preservation rule) with the
full reasoning and real examples behind each one — read it before writing
anything nontrivial, and definitely before disagreeing with something
above.
