# FFS Explorer

A forensic viewer for GrayKey and Cellebrite UFED iOS and Android full file system zips. Browse without unpacking, then review the media, search, and read the artifact reports.

## Install

### Windows program

Download the latest release from [GitHub releases](https://github.com/KlasScripts/FFS-Explorer/releases/latest). The file is named `ffs-explorer-windows-v*.zip`. Unzip it and run `ffs-explorer.exe`.

There is no Mac or Linux build. On those systems, run from source.

### From source

Python 3.12, on Windows, Mac, or Linux.

```
git clone https://github.com/KlasScripts/FFS-Explorer.git
cd FFS-Explorer
```

Windows:

```
py -3.12 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python ffs-explorer.py
```

Mac and Linux:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python ffs-explorer.py
```

On a Mac, the usual PyAV install cannot decode HEVC video thumbnails.

## The window

You open the zip and stay in it. The main window has five tabs.

- **File Browser.** The folders and the files in the folder you select. This is where you see paths, times, types, and the columns for the files in view.
- **Media Browser.** The pictures and videos, as thumbnails, with a full-size view and a scrub across a video.
- **Keyword Search.** A word or phrase across the archive, unpacked nested archives, indexed LevelDB folders, file and folder names, and the picture and video text saved by Process Case.
- **Artifact Viewer.** The reports from the app parsers, such as messages. Where a report interprets a stored value, its notes page says what was assumed.
- **Timeline.** The report events whose meaning a parser has stated, and the file times of user media, in time order.

Selecting a file opens it in the preview underneath. That preview has four tabs, and it only reads the archive.

- **Hex.** The raw bytes.
- **Text.** The file as text, including property lists, JSON, XML, Android binary XML, and protobuf.
- **Database.** A SQLite file, recognised by the `SQLite format 3` header rather than the file name. Tables, a paged row view, and the write-ahead log as a net change: rows the log added, deleted, or modified, with a modified cell shown as `old → new`.
- **SEGB.** Apple's segmented-buffer records, such as Biome, recognised by the `SEGB` header. Each record's time, state, and size, with the payload decoded as protobuf. Known streams use a built-in schema, and a corrected schema can be saved in the case.

SEGB record parsing uses the MIT-licensed [`ccl_segb`](https://github.com/abrignoni/iLEAPP/tree/main/scripts/ccl_segb) library by Alex Caithness of CCL Forensics, vendored under `app/ccl_segb/`. The built-in stream schemas are ported from [iLEAPP](https://github.com/abrignoni/iLEAPP).

## Process Case

Opening a case detects the layout of the zip, copies the zip's directory into the case folder, and builds the file tree. That result is saved, so opening the same case again uses the saved tree.

| Layout | Where the file details come from |
|---|---|
| Cellebrite iOS | `filesystemN/` plus `metadata.msgpack` |
| GrayKey iOS and GrayKey Android | The zip extra fields |
| Cellebrite Android, `Dump/` | The zip extra fields under `Dump/` |
| Cellebrite Android, `filesystemN/` | The same detection path as Cellebrite iOS |

On iOS, an app container's GUID is shown with its bundle id. The path rules are in `ASSUMPTIONS.md`.

Process Case is a separate step after the tree is up. Nothing in it runs until you press Run, and a stage runs only if it is ticked. A stage that has already been done starts unticked, so a later run does not repeat it unless you tick it again. The stages run in this order, because a later stage uses what an earlier one unpacked or typed.

**Check data integrity.** Hashes the zip and compares it with the hash recorded at acquisition: the `.ufd` beside a Cellebrite archive, or the PDF report beside a GrayKey archive. A mismatch means the zip has changed since it was acquired. If no hash file is there, the check is skipped and the later stages still run.

**Header scan.** Reads the start of a file and sets its type from that, instead of from the file name. A database named `.bin`, or a picture with no extension, is then shown as what it is. Tier 1 reads files with no recognised extension, inside the app and user folders. Tier 2 reads every file in those folders. Tier 3 reads every file in the user partition. A tier already run is marked as done. You can move up a tier. You cannot run a lower one again from here.

**Find and select archives.** Looks for zip and similar archives in the app, iCloud, Files, Mail, and message locations, then asks which ones to unpack. The unpacked files are available to the stages after this one. Archives with no extension are only found once a header scan has typed them.

**Index LevelDB and IndexedDB.** Reads the LevelDB and IndexedDB folders, such as browser local storage and app caches, into a search index. Keyword Search can then find text inside those records. Folders already indexed are left alone. A folder that cannot be read is reported, and Keyword Search does not cover it.

**Scan databases, property lists, and web cache for embedded media.** Opens SQLite databases and property lists and pulls out pictures and video stored inside them, including in freed space and the write-ahead log, not only the live rows. It also opens Chromium web-cache records and shows the cached picture or video with its source address. The scope is the app and user folders, or the whole user partition. A known-noise list can leave out databases that hold no useful media. Files unpacked from nested archives are included.

**Extract media metadata.** Reads the text stored with pictures and videos: taken date, camera, GPS, duration, dimensions, and the other saved fields. Those become columns on the File Browser, and Keyword Search can search them. It covers pictures and videos in the folders marked for processing, plus files unpacked or recovered by the stages above.

**Detect duplicate files.** Groups files that share a size and a CRC32. Groups that contain a picture or video are then read and confirmed with an MD5, and the File Browser shows a duplicate count and that hash. The other groups are listed as possible duplicates. Untick that option to confirm every group. On a GrayKey archive, a file whose SHA-256 was recorded at acquisition is grouped on that hash with no extra read.

## For developers

`CLAUDE.md` is the map of the code. `ASSUMPTIONS.md` is the record of facts and decisions. `WRITING_ARTIFACT_PARSERS.md` is how to add a report. Open work is tracked in [ffs-notes](https://github.com/KlasScripts/ffs-notes/issues).

## Licence

FFS Explorer is free software under the [GNU General Public License v3.0](LICENSE). The licence is the warranty statement.
