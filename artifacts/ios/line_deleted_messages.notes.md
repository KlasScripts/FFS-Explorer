# LINE — Deleted & Changed Messages (iOS): read this first

This report lists LINE messages that are **no longer in the app's message
table, or that changed after they were stored**, recovered from the iPhone's
LINE database (`Line.sqlite`). One row is one recovered record, exactly as
found in one table. It is **not joined** to chats or contacts: `chat_id` and
`sender_id` are the stored ids.

Most columns are copied straight from the record. Three are **interpreted**:
`status`, `direction` and the word in `content_type`. They're explained below.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **A recovered record is not proof of when it was deleted** | It shows the message existed and is gone (or changed); the time is when it was stored, not when it was removed. | [Status](#status) |
| **"Changed" rows are filtered** | An earlier version is listed only when the text, kind, sender, chat or location changed. Versions that changed only delivery fields are counted in the run log, not listed. | [Changed messages](#changed-messages) |
| **Some records have no row id** | A record found by its pattern in free space has lost its row id. It can't be matched to a row number. | [How found](#how-found) |
| **Chat previews are not messages** | A chat's earlier last-message preview is listed only when its text is in no message. Most are app-generated (calls, locations). | [Chat previews](#chat-previews) |
| **Names are not resolved** | There is no join: look up `chat_id` / `sender_id` in the Database tab (`ZCHAT.Z_PK`, `ZUSER.Z_PK`). | [Not in this report](#not-in-this-report) |

---

## Status

| Status | Meaning |
|---|---|
| **Deleted** | The record is not in the message table any more. Found in the WAL, free space, a freed page, overflow pages, or by its record pattern. |
| **Changed** | An earlier version of a message that is still in the table. The current value is beside it as `message (now)` etc., and `changed_fields` names what changed. |
| **Unfinished write / Unfinished delete** | The app was writing when the phone was acquired (a hot journal). Never committed. |

> **Example (iOS 17 test image).** Josh Hickman's image log records two LINE
> messages received and then deleted (2023-12-24 16:42 → deleted 16:43, and
> 2024-04-27 10:11 → deleted 10:17, US Eastern). Both are in this report as
> **Deleted / Incoming**, at 21:42:36 and 14:11:56 UTC, found as earlier
> versions in the WAL.

#### ✅ Validation
- **Both documented deletions recovered:** *verified on ground truth* (iOS 17 image, 2026-11-17).
- **Recovery methods:** *verified* on the SQLite Forensic Corpus (`tests/test_sqlite_corpus.py`).

## Changed messages

LINE rewrites a message row as it is sent and delivered (send status,
timestamp, metadata). Those versions are not shown: on the test image 27 of
31 earlier versions changed only those. A version is listed when one of these
changed: `message`, `content_type`, `sender_id`, `chat_id`, `latitude`,
`longitude`. The run log says how many others were left out.

#### ✅ Validation
- *Observed on the test image only*: two earlier versions with a changed location were listed.

## How found

`how_found` is the carver's own description: for example *older version in
the WAL*, *deleted since the last checkpoint*, *record header rebuilt from
this table's pattern; unverified match*, or *table not certain*. A record
found by pattern has no `row_id`. `table_certain` is False when the page may
have belonged to another table.

## Direction and content type

| Value | Meaning | How shown | Confidence | Basis |
|---|---|---|---|---|
| `ZSENDER` empty | sent by this phone | **Outgoing** | high | 23 of 24 documented sent messages (iOS 17 image) |
| `ZSENDER` set | received | **Incoming** | high | 21 of 22 documented received messages |
| `ZCONTENTTYPE` 0 / 1 / 100 | text / picture / location | `text (0)` etc. | high | 37 / 4 / 3 documented messages |
| other `ZCONTENTTYPE` | not identified | the number | — | 6 seen twice, not matched |

The two direction exceptions each share their minute with a message the other
way, so the minute-level log can't tell them apart.

## Chat previews

`ZCHAT.ZLASTMESSAGE` is the text shown in the chat list. Earlier versions of
it sit in the WAL. One is listed only when its text is in no message, live or
recovered: then it may be the only trace of a deleted message. On the test
image the ten listed were app-generated (call durations, a location
placeholder, an official account's promotions); both deleted messages'
previews were left out because the messages themselves were recovered.

## Not in this report

- **Live messages and calls**: the LINE Messages (iOS) report.
- **Chat and contact names**: no join. Open the Database tab on `Line.sqlite`.
- **Calls**, other than as chat previews.

## Sources

- Josh Hickman, iOS 17 test image and its creation log (thebinaryhick.blog);
  transcribed in `test_data/josh_hickman_ios17/`.
- SQLite file format (sqlite.org/fileformat2.html).
