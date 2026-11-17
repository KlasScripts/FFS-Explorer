# LINE Messages (iOS): read this first

This report lists LINE messages and calls from the iPhone's LINE database
(`Line.sqlite`, table `ZMESSAGE`). One row is one message or call: the
conversation, when, the direction, who sent it, the text, and any picture or
location.

Most columns are copied straight from the database. Five are **interpreted**:
`direction`, `sender` (for messages this phone sent), `event`, `conversation`
and `call_started`. They're explained below. Deleted and changed messages are
in the separate report **LINE — Deleted & Changed Messages (iOS)**.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **A call's stored time is when it ended** | `timestamp` on a call row is the end; `call_started` is worked out as end minus duration. | [Calls](#calls) |
| **Direction is inferred** | A message with no sender reference is read as sent by this phone. | [Direction](#direction) |
| **Deleted messages are not here** | They're in the Deleted & Changed report. On the test image both documented deletions are there, not here. | [Deleted messages](#deleted-messages) |
| **One account per run** | The report reads the first `PrivateStore/P_<id>` store found. A phone with two LINE accounts would need checking. | [Not in this report](#not-in-this-report) |
| **Group chats are untested** | The test image has none, so a group's name and members are not checked. | [Conversation](#conversation) |

---

## Direction

`ZSENDER` is empty on a message this phone sent: **Outgoing**, sender
`[local user]` (the store keeps no user row for the account itself). Set:
**Incoming**, sender is that user's name.

#### ✅ Validation
- *Verified on ground truth* (iOS 17 test image): 23 of 24 documented sent
  messages have no sender, 21 of 22 received ones have one; the two others
  share their minute with a message the other way.
- *Published:* iLEAPP's iOS `line.py` makes the same reading.

## Calls

A call is a message of content type 6. Its `ZCONTENTMETADATA` is a keyed
archive with `TYPE` (A = audio, V = video), `RESULT`, `CAUSE` and `DURATION`
(milliseconds). `event` reads e.g. `Outgoing audio call, 1:36`; a result other
than NORMAL is added as stored.

#### ✅ Validation
- *Verified on ground truth:* all 8 documented calls (iOS 17 image) match on
  start minute, direction and audio/video.
- *Published:* iLEAPP's `lineCalls` (four images), cross-checked there with the
  separate `u<MID>_CallHistory.db` call record.

## Event and content type

| `ZCONTENTTYPE` | `event` | Confidence | Basis |
|---|---|---|---|
| 0 | (blank: a text message) | high | 37 documented messages |
| 1 | `Image` | high | 4 documented pictures; the file is linked in `attachment_path` |
| 6 | `… call, m:ss` | high | 8 documented calls |
| 100 | `Location` (`latitude` / `longitude` in degrees) | high | 3 documented locations |
| 17 | `Card message (content type 17)` (`card_text` = its ALT_TEXT) | low | named from its metadata keys (MARKUP_JSON, ALT_TEXT); not in the ground truth; all 4 from an official-account chat |
| other | `Content type N` | — | not seen |

## Conversation

The other party of a chat is the user whose `ZMID` is the chat's `ZMID`. A
chat with no such user (on the test image, the official-account chat) shows
`[no chat contact record — raw ZCHAT.ZMID=…]`.

#### ✅ Validation
- *Observed:* 4 of the 5 chats on the iOS 17 image resolve; group chats not
  present, so not tested.

## Deleted messages

Two LINE messages were documented as received and then deleted on the iOS 17
image. Neither is a live row here; both are in **LINE — Deleted & Changed
Messages (iOS)**, recovered from the WAL. Together the two reports account
for all 39 documented sent and received messages.

## Not in this report

- Deleted and changed messages: the Deleted & Changed report.
- A second LINE account on the same phone (only the first store is read).
- Reactions (no reaction column found in `ZMESSAGE`).
- The `NCXVoIP` call-history file, synced contacts, encryption records
  (iLEAPP's `linePrivateStore.py` covers some of these).

## Sources

- Josh Hickman, iOS 17 test image and its creation log; transcribed in
  `test_data/josh_hickman_ios17/`.
- iLEAPP `scripts/artifacts/line.py` and `linePrivateStore.py` (abrignoni/iLEAPP).
