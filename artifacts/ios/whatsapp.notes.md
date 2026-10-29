# WhatsApp Messages (iOS): read this first

This report lists WhatsApp messages from the iPhone's own WhatsApp database
(`ChatStorage.sqlite`). One row is one message: who sent it, when, its text,
and any photo, video or file attached.

Most columns are copied straight from the database. A few are **interpreted**,
meaning the app works out what a value means. Those are the ones that can
mislead you, and they're explained below.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **A deleted message often leaves no text** | When the phone's user picks "Delete for me", the message is removed from the database. This report can show that *something* is missing and roughly when, but not what it said. | [Deleted messages](#deleted-messages) |
| **Disappearing messages delete themselves** | A missing message isn't proof the user deleted it. If disappearing messages was switched on, WhatsApp removed it automatically. Check the *Disappearing* columns before drawing conclusions. | [Disappearing messages](#disappearing-messages) |
| **Some message types are our interpretation** | WhatsApp doesn't publish what its type numbers mean. A few (deleted for everyone, timer changes) are worked out from research and test data. | [Message types](#message-types) |
| **"Chat members" is who was in the chat at extraction** | People join and leave groups. The member list isn't a list of who received any particular message. | [Chat members](#chat-members) |
| **Calls aren't in this report** | WhatsApp keeps call history in a separate database. | [Not in this report](#not-in-this-report) |

---

## Deleted messages

There are several ways a WhatsApp message can be deleted, and each one leaves
different traces on the phone.

| What happened | Who can do it | What's left on this phone | How it shows in FFS Explorer |
|---|---|---|---|
| **Delete for me** | This phone's user | Nothing. The message row is removed. | A gap in the chat's message numbers: **Missing (Gaps)** and **Deletion Windows** in the Chat Groups summary |
| **Delete for everyone** | Whoever sent the message | A placeholder row: time, chat and sender are kept, the text isn't | Event: `Deleted for everyone — content not kept`, counted in **Deleted For Everyone (Me/Them)** |
| **Delete for me on the *other* person's phone** | The other person | Nothing changes on this phone | Not visible here. The message is still in this report. |
| **Clear chat / Delete chat** | This phone's user | *Not yet tested.* Clearing should leave the chat with its counter intact. Deleting a chat removes it entirely. | Expected as **Missing At End**, or a missing chat. Treat as unconfirmed. |

### How the gaps work

Every message in a chat gets a running number (`ZSORT`): 1, 2, 3 and so on.
The chat also keeps a counter of how many numbers it has used
(`ZMESSAGECOUNTER`). That counter doesn't go down when a message is deleted.

- **Issued / Present 30 / 28** means 29 numbers were used (the counter holds the next number) and 28 messages are left, so one is missing.
- **A gap** (…22, 23, 25…) means a message existed between two that survive. The two surviving messages' times give the **Deletion Window**: the missing message was sent in that window.
- **Missing At End** means numbers were used after the last surviving message. Those messages were removed later on (cleared, deleted, or expired).

> **Example (iOS 17 test image, chat "Liz Dehner").** Number 24 is missing between
> "It's gone." (2024-01-30 20:11:13 UTC) and "I deleted." (20:18:11 UTC).
> The image's creation log says a message was received at 15:13 local (20:13 UTC)
> and deleted at 15:14 with "Delete for me". That matches exactly.

### Can the text be recovered?

Usually not from this database. On the test images, freed space held no deleted
text, the WAL was empty, and WhatsApp's search index (`ChatSearchV5f.sqlite`)
had dropped the deleted message too. Better places to look:
- **the other person's phone** (the best chance);
- **iCloud or computer backups** made before the deletion;
- **notification records**, which may hold the text of incoming messages.

#### ✅ Validation
- **Gaps mean deleted messages:** *published* (Belkasoft: missing `ZSORT` numbers "may indicate that some messages… were deleted 'for Me'") and *verified* against the iOS 17 ground truth above.
- **The counter is a running total:** *published* (Belkasoft: "the number of messages exchanged") and *observed*: it stayed at 30 after the iOS 17 deletion.
- **Not tested:** what "Clear chat" and "Delete chat" leave behind.

---

## Disappearing messages

Either person in a one-to-one chat (or an admin in a group) can switch on
**disappearing messages**. New messages are then deleted automatically after
**24 hours, 7 days or 90 days**. Messages sent before it was switched on aren't affected.

This matters because **expired messages leave the same gaps as deleted ones**.
Before saying "the user deleted messages", check whether a timer explains them.

### What FFS Explorer shows

| Where | Column | What it tells you |
|---|---|---|
| Report | **Disappears at** | When this message was due to vanish. Empty means no timer applied. |
| Report | **Event** | A setting change shows as `Disappearing messages set to 90 days by me…` |
| Chat Groups | **Disappearing** / **Set By** / **Set At** | The current setting, who chose it, and when |
| Chat Groups | **Setting History** | Every change found, oldest first |
| Chat Groups | **Missing Consistent With Timer** | Missing messages a timer explains |
| Chat Groups | **Missing Not Explained** | Missing messages *no* timer explains. They were removed on this phone some other way, e.g. "Delete for me". |

**How sure is "consistent with timer"?**
- **For a gap, it's certain.** The surviving message after the gap proves when the missing ones were sent, so we can check that they'd have expired before the phone was acquired.
- **For messages missing at the end, it's only consistent.** We don't know when they were sent, and one sent the day before acquisition wouldn't have expired yet.

> **Example (CTF23 test image, chat "Felix Davey").** Felix set a 7-day timer on
> 2023-06-03 16:21:31 UTC. The chat used 8 message numbers but only 2 are left.
> The phone was acquired on 29 June, so the 6 later messages are **consistent with
> having expired**. They're not evidence the user deleted anything.

#### ✅ Validation

| What we read | Where it's stored | Status | Basis |
|---|---|---|---|
| Message expiry time | `ZWAMESSAGE.ZMESSAGEERRORSTATUS` (Apple time; 0 = no timer) | **Published** and observed | Heath, MacDermott & Akinbi (2023), Fig. 5; on CTF23 every later message expires exactly 90.00 days after it was sent |
| A setting change | A message of type **28** | **Observed only, not published** | 2 examples on CTF23; no paper, blog, iLEAPP or ALEAPP covers it |
| Who changed it | `ZISFROMME` on that type-28 message | **Observed only** | Both examples fit: owner set 90 days, Felix set 7 days |
| Timer length | Field 36 of the type-28 message's `ZMETADATA` | **Observed only** | Decodes to exactly 7,776,000 s (90 days) and 604,800 s (7 days) |
| Switched **off** | Probably a type-28 message with 0 | **Not yet seen** | Needs a test extraction |

*Use the "observed only" rows as strong leads, not settled fact. Confirm them on a
controlled test phone before relying on them in court.*

---

## Message types

WhatsApp gives every message a type number (`ZMESSAGETYPE`) and doesn't document
what the numbers mean. The table shows what this report assumes, and why.

**Direction** comes from `ZISFROMME`, except type 6 (System, above). On type 59 and type 10 rows it is shown as stored, but whether `ZISFROMME` says who acted on those rows has not been checked (IOS17 image: type 59 is 2 Sent and 2 Received, type 10 is 7 Received). Do not read Direction on those rows as who did something.

**Message** is always `ZTEXT` exactly as stored. When a row has no text, the
label below is in the **Event** column, never in Message, so a search or a
quote of the message never finds this report's own words.

| Type | What it is | Event when there is no text | Confidence | Basis |
|---|---|---|---|---|
| 0 | Text | The text | High | Belkasoft |
| 1 | Image | `Media attachment…` + thumbnail | High | Belkasoft |
| 2 / 3 | Video / voice note | Caption in Message, or `No text — message type N` | High | Belkasoft |
| 4 / 5 | Contact card / location | `No text — message type N` | High | Belkasoft |
| 6 | Group event | `No text — message type 6`. Direction is **System**, not Sent or Received: a group event is not a message either side sent. `is_from_me` shows the stored `ZISFROMME`. | Medium | Belkasoft ("group") |
| 7 / 8 | Link / file | The text in Message, or `No text — message type N` | High | Belkasoft |
| **14** | **Deleted for everyone** | `Deleted for everyone — content not kept` | **Medium** | WhatsApp-Chat-Exporter treats 14 as "Message deleted"; one CTF23 example ("Sorry wrong pic" right after) |
| **28** | **Disappearing-messages setting change** | `Disappearing messages set to …` (always, even with text) | **Low–medium** | Observed only, see above |
| **59** | Something next to calls | `System event, type 59…` | **Low** | Every iOS 17 example sits next to a documented call. Not confirmed. |
| other | Unknown | `No text — message type N` | — | The type number as stored, never guessed |

Group events (`ZGROUPEVENTTYPE`, such as joins and leaves) are shown as stored. No
public source decodes them (iLEAPP doesn't either).

---

## Who is "Me"

"Me" is the phone's own WhatsApp account. iOS doesn't store it in one obvious
place, so FFS Explorer takes the **most common recipient of incoming one-to-one
messages** (`ZTOJID`).

#### ✅ Validation
- **iOS 17:** 19195794674. That's the phone number in the image's creation log (919-579-4674). **Verified.**
- **CTF23:** 12065937224. **Observed**; there's no creation log to check against.

---

## Chat members

The **Chat Groups** summary lists everyone in each conversation: you first, then
the other person, or every group member WhatsApp lists (`ZWAGROUPMEMBER`).
Members marked *(inactive)* were in the list but no longer active.

- **This is the membership when the phone was acquired.** Someone in the list may have joined after a message was sent, or left before it.
- **That's why no message row claims who received it.** The membership history isn't reliable enough for that.
- **"[group members not recorded]"** means WhatsApp kept no list. On iOS 17, the groups the owner had left show only the owner.

---

## Not in this report

- **Calls:** WhatsApp keeps these in a separate call-history database. Type-59 rows only hint that a call happened.
- **Status updates:** they expire after 24 hours, so usually nothing is left.
- **Earlier versions of edited messages:** not checked for WhatsApp yet. Assume only the current text is shown.

---

## Sources
- Belkasoft, *iOS WhatsApp Forensics with Belkasoft X*, belkasoft.com/ios-whatsapp-forensics-with-belkasoft-x
- Belkasoft, *Forensic Duel: Exploring Deleted WhatsApp Messages, iOS vs Android*, belkasoft.com/exploring-deleted-whatsapp-messages
- WhatsApp, *Introducing Disappearing Messages on WhatsApp* (who can switch it on), about.fb.com/news/2020/11/introducing-disappearing-messages-on-whatsapp
- H. Heath, Á. MacDermott, A. Akinbi (2023), *Forensic analysis of ephemeral messaging applications: Disappearing messages or evidential data?*, Forensic Science International: Digital Investigation 46, 301585
- WhatsApp-Chat-Exporter (KnugiHK), iOS handler: type 14 = "Message deleted", github.com/KnugiHK/Whatsapp-Chat-Exporter
- Test images: Josh Hickman iOS 17 (with creation log); CTF23 iOS 16.5 (no creation log)
