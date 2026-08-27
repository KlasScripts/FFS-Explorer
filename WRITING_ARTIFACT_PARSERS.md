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
    import sqlite3
    conn = sqlite3.connect(paths["main_db"])
    conn.row_factory = sqlite3.Row

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
present but never block the run — always list a database's `-wal`/`-shm`
sidecars here, since a database in WAL mode can have its newest rows only
in the `-wal` file (never in the main file).

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
Unix-ms yourself and then declaring `"ms"`). A `recoverable_tables`-carved
row carries the *raw* column value straight from the table, in its
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

### `media_fields` (if the app has photos/video/attachments)

```python
media_fields = ["attachment_path"]
```

Names the output field(s) holding a full archive **ui_path** (not a raw
database value, not a filesystem path) to a media file. The Report table
then shows a thumbnail there, with double-click opening it full-size or
playing it. Build the ui_path in `run()`:

```python
attachment_path = f"{paths['_app_base_ui_path']}/{row['media_column']}"
```

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
cell to view. The output field the entry points at usually already needs
to exist for something else (a raw id you're already keeping per the rule
above) — but sometimes doesn't, and you'll need to add one more `SELECT`
column purely to make this work (see `artifacts/ios/whatsapp.py`'s group-
member/media-item entries for a real example of exactly that). **Don't
guess this one** — declaring it wrong doesn't fail loudly, it silently
points the examiner at the wrong bytes, which is worse than not having
the feature at all. If you can't confirm a table's rowid is real, leave
that entry out; the report still works, that row's Record mode just says
"not available."

A row your `recoverable_tables` recovery pass carved (below) still has a
real, exact citation too — the carving pass already knows precisely which
bytes it found, in either the main db file or a `-wal` sidecar — so
`record_source` covers both live and recovered rows for free once
declared; nothing extra to write for the recovered case.

### `recoverable_tables` (if you've checked for deleted content)

```python
recoverable_tables = ["messages"]
```

Declares a table to also carve for deleted rows (freeblocks, freed pages,
WAL history). No recovery code belongs in the parser — this declaration is
the entire contribution; the shared carving pipeline does the work and
appends whatever it finds to your `run()` output. Only add this once
you've actually checked (a real negative — "no WAL, checked, found
nothing" — is still worth declaring, so a future re-check on different
data finds it automatically instead of needing another one-off
investigation).

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
