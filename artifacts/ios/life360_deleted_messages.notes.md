# Life360 — Deleted & Changed Messages (iOS): read this first

This report lists Life360 chat messages recovered from the iPhone's Life360
database (`Messaging.sqlite`) that are **no longer in its message table, or
that changed after they were stored**. One row is one recovered record,
exactly as found. It is **not joined** to members or threads: `sender_id` and
`thread_id` are the stored ids.

Three columns are **interpreted**: `status`, `direction` and the word in
`deleted_in_app`. They're explained below.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **"Deleted" means not in the table now** | On the test image the message table held no live messages at all, so every message shows as Deleted. That is not proof the user deleted them. | [Status and the deleted flag](#status-and-the-deleted-flag) |
| **`deleted_in_app` is the app's own deletion mark** | `Yes (1)` is set by Life360 on a message it recorded as deleted. On the test image exactly the two documented deletions carry it. | [Status and the deleted flag](#status-and-the-deleted-flag) |
| **A message can carry two ids** | `life360_message_id` and `client_message_id`: a record found before the server's id was assigned may show the client id. | [Ids](#ids) |
| **Names are not resolved** | No join: look up `sender_id` in `ZCHATMEMBER.Z_PK` and `thread_id` in `ZCHATMESSAGETHREAD.Z_PK` (Database tab). | [Not in this report](#not-in-this-report) |

---

## Status and the deleted flag

| Column | Value | Meaning |
|---|---|---|
| `status` | **Deleted** | The record is not in `ZCHATMESSAGE` now (found in the WAL). |
| `status` | **Changed** | An earlier version of a message that is still there (none on the test image: the table was empty). |
| `deleted_in_app` | **Yes (1)** | `ZISLOCALLYDELETED` = 1: the app marked this message deleted. A message whose flag changed has two rows, one with No and one with Yes. |
| `deleted_in_app` | **No (0)** | Not marked deleted in that version. |

> **Example (iOS 17 test image).** Josh Hickman's image log records two
> Life360 messages received and then deleted (2024-01-06 15:54 → deleted
> 15:56, and 2024-04-23 09:46 → deleted 09:50, US Eastern). Both are in this
> report with a version marked `deleted_in_app` = Yes (1), and no other
> message carries that mark.

#### ✅ Validation
- *Verified on ground truth:* both documented deletions recovered and flagged;
  30 of 31 documented sent/received messages recovered at their documented
  minute (not: the last one, 2024-04-23 09:57).
- **Why the whole table was empty** was not established (not in the log).

## Direction

**Outgoing** when `sender_id` is a member the app marks as the logged-in user
(`ZCHATMEMBER.ZISLOGGEDINUSER` = 1; one such member per circle), else
**Incoming**. The member ids differ per phone, so they are looked up in that
table; no other member field is added.

#### ✅ Validation
- *Verified on ground truth:* right on all 30 recovered documented messages.

## Ids

On the test image the 247 recovered records are 33 messages. They carry 46
distinct `ZMESSAGEID` values because 13 records of sent messages hold the
app's client id (`ZCLIENTMESSAGEID`) where the server's id later appears.
Each message is listed once per meaningful version (`version_fields`: text,
sender, thread, type, deleted flag).

## Not in this report

- Calls and check-ins (documented on the image, not chat messages).
- Member and thread names: no join.
- Photos: `photo_id` only (`ZCHATMESSAGEPHOTO` held no live rows).

## Sources

- Josh Hickman, iOS 17 test image and its creation log; transcribed in
  `test_data/josh_hickman_ios17/`.
