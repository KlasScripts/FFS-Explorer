# WhatsApp Messages (Android): read this first

This report lists WhatsApp messages from `msgstore.db` (table `message`). One row is one message still in the database: who sent it, when, its text, and any file WhatsApp still points at.

Most columns are copied from the database. A few are interpreted. The one that matters for a cleared chat is below: **this report counts messages that are still there.** It does not say how many were removed.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Clearing a chat does not look like a count of deleted messages** | The conversation can still be there with almost nothing in it. A last-message time and a clear time were still in the database on one test. This report shows neither as a clear. | [Clearing all messages](#clearing-all-messages) |
| **A local delete often leaves no row** | "Delete for me" removes the message. This report has no gap counter for Android, so a missing message is easy to miss. | [Other ways a message is deleted](#other-ways-a-message-is-deleted) |
| **Carving often finds nothing** | An empty result is not proof that nothing was deleted. On the extraction this parser was built against, freed space was zeroed. | [Recovering deleted text](#recovering-deleted-text) |
| **"Chat members" is who was in the chat at extraction** | The member list is not who received any particular message. | [Chat members](#chat-members) |
| **A LID is not a phone number** | An address ending in `@lid` is a privacy id. The phone number is a different address, ending in `@s.whatsapp.net`, and only when `jid_map` links the two. They are separate columns. A chat with no name is titled with the phone JID when that link exists. | [Phone JID and LID](#phone-jid-and-lid) |
| **There is no gap counter on this page** | Android `msgstore.db` has no per-message sequence and no disappearing-message setting column. Those headers are left off this page. A blank cell is not standing in for them. | [Chat Groups columns](#chat-groups-columns) |
| **A reaction notifies like a message** | A reaction posts a notification for the chat, but it is not a message row. It is in the Reactions column of the message it was added to. | [Reactions](#reactions) |
| **A pin is not always where the phone was** | Coordinates with no place name are what "Send your current location" stored. A place picked from the list stores a name, an address, and a link. Those coordinates are the place the user chose. | [Current location and a selected place](#current-location-and-a-selected-place) |

---

## Clearing all messages

**Limited testing.** One linked Android WhatsApp (a companion phone, not the primary phone), one chat, one **Clear chat → All messages** on 26 October 2026. Deleting the thread was a separate action, once, the same day. Do not treat either as general WhatsApp behaviour.

Before the clear, that chat had **38 messages**. After it:

- The chat was still in `chat`. It still opened, with the encryption notice and an empty message box.
- All 38 message rows were gone, including an earlier "deleted for everyone" marker. Nothing in this report says "38 before, 0 now."
- The pictures and videos that belonged to that chat were removed from WhatsApp's media folders and from the gallery index. Their filenames were gone from `msgstore.db`.

### Last message time

`chat.sort_timestamp` stayed at **26 October 2026, 13:06:18 UTC**. That was the time of the newest message in the chat before the clear.

That message happened to be one the phone user sent. The column does not say incoming or outgoing, and after the clear the message row that would have said so is gone. On this phone the same column matched the newest message, either direction, on 74 of 80 chats. Six chats had a later sort time than any message still stored. Those six were not examined.

This report does not read `sort_timestamp`. The 13:06 time is not in the report table.

### When the chat was cleared

WhatsApp wrote a new system row at **26 October 2026, 17:46:13 UTC**, the moment Clear chat was pressed. On screen that row is the encryption notice. It is not a column named "cleared at."

This report shows that row as one ordinary sent message with no text, timed 17:46. It does not label it as a clear, and it does not show the 13:06 last-message time.

#### ✅ Validation

| What we read | Where it is stored | Status | Basis |
|---|---|---|---|
| Last message time survives a clear | `chat.sort_timestamp` | **Observed only. Limited test.** | One clear, one chat, linked Android phone, 26 October 2026. Matched the newest message (outgoing). Direction is not stored on the chat row. |
| Either direction | Same column, other chats on that phone | **Observed only. Not fully checked.** | 74 of 80 chats matched the newest stored message (51 incoming, 23 outgoing). 6 mismatches not examined. |
| Clear time | Timestamp of the new system row (encryption notice) | **Observed only. Limited test.** | Same single clear. The time matches when Clear chat was pressed. |
| Previous message count | Not stored after the clear | **Observed only. Limited test.** | 38 rows in a database taken before the clear. None of those ids remained. |
| Shown in this report | — | **Not shown** | The report lists the one system row at 17:46 and does not list `sort_timestamp`. |

---

## Other ways a message is deleted

Same phone, same day, still limited. Each of these was done once.

| What happened | What was left | How it shows in this report |
|---|---|---|
| **Delete for everyone** (the other person) | The message stays, type **15**, with a `message_revoked` row. That file, its thumbnail, and its gallery entry were removed. | **Deleted for everyone**. The text and file are not there. |
| **Delete for me** (this phone, one message, including its media) | That message row is removed. No type 15, no `message_revoked` row. The file and thumbnail were removed. The rest of the chat stayed. | The row is absent. This report does not flag the gap. |
| **Clear all messages** | See [Clearing all messages](#clearing-all-messages). | One empty system row. |
| **Delete the chat thread** | The old chat row was removed. Its messages, thumbnails, and that chat's own media files were gone. A hidden empty chat row for the same address remained, plus one row each in `integrity_deleted_chat_message_count` and `integrity_deleted_chat_metadata`. The count stored there was not the number of messages that had been in the chat. The creation time was kept. | The old thread is absent. The hidden row has no messages, so this report does not list it. |

#### ✅ Validation

- **Delete for everyone, delete for me, and delete the chat:** one example each, linked Android phone, 26 October 2026. Observed only.

---

## Recovering deleted text

On the extraction this parser was built against, `msgstore.db` had `PRAGMA secure_delete = FAST`. Freed page bytes were zero, apart from a 2-byte fragment, including the page of one confirmed deleted message. The full-text table and the live WAL did not hold a copy either. Finding nothing by carving is a real negative for carving. It is not proof that nothing was deleted.

On the linked phone used for the clear above, `auto_vacuum` was on and the freelist was empty afterwards. The deleted filenames were not in the database file.

---

## Phone JID and LID

WhatsApp stores a person as one of two addresses.

- **Phone JID** — ends in `@s.whatsapp.net`. This is the phone number.
- **LID** — ends in `@lid`. This is a privacy id. It is not the phone number, and the part before `@lid` is not a phone number either.

`jid_map` links them. `lid_row_id` is the LID. `jid_row_id` is the phone JID. Checked on the Android 14 test image (1,701 map rows) and on a linked Android companion (172 map rows): every `lid_row_id` is server `lid`, every `jid_row_id` is server `s.whatsapp.net`, and none of the pairs share the same user part. The digits in a LID are not the phone number. Reversing the two columns pairs the wrong rows. A phone number is written only when that map row exists.

The contact name for a LID is stored on the phone JID. Joining the contact table on the LID alone finds no name.

`phone_jid` and `lid` are the other person on that row, for a one-to-one chat and for a group. When both forms are stored, both columns are filled. A phone with no LID leaves `lid` blank. A group message this phone sent leaves both blank.

`chat_jid` is the conversation. On a group that is the `@g.us` address, which is neither a phone nor a LID. On a one-to-one chat it is the address the chat row was stored under.

On the Android 14 test image the chats and group senders are phone JIDs (jid type 0). A one-to-one row has `phone_jid` matching `chat_jid`, and `lid` empty. An incoming group row has the sender's phone in `phone_jid` and `lid` empty. The map is still in the database for other rows. Those chats do not use it.

On the linked companion the two forms are both in use. 59 one-to-one chats are phone JIDs, with the contact name on that address. 18 are LIDs (jid type 18) with a map row: `chat_jid` and `lid` are the LID, `phone_jid` is the mapped phone, and the contact name is on the phone. One LID chat has no map row: `phone_jid` is left empty and the title stays the LID. Incoming group messages are mostly LIDs (521 mapped to a phone; the name is on the phone, not on the LID) plus 10 that are already phone JIDs. Those rows use the same `phone_jid` and `lid` columns. The Chat Groups participant is the contact name, then the phone, then the LID.

`lid_me` is a third thing. One jid row, server `lid_me`, raw string `lid_me`, type 11. It is not `@lid` and it is not a phone number. On the companion it is the member-list slot for this phone: 29 of 31 groups use it, and those groups do not also list `registration_jid` or `self_lid`. The two groups without it have no members at all. This report does not list `lid_me` as another person. Me stays `registration_jid`. The Android 14 test image has no `lid_me` row, and its groups do not list the registered number either. `self_lid` is a real `@lid` (type 18). On the companion it had no `jid_map` row and it was not a group member, so it is not given the registered number.

## Chat Groups columns

This page shows a column only when the Android report has that data. `msgstore.db` on Android has no per-message sequence counter and no disappearing-message setting column of the kind those headers need, so these are not on this page:

- Issued / Present, Missing (Gaps), Missing At End, Deletion Windows
- Deleted For Everyone (Me/Them). A type 15 row still counts in **Deleted**. The Android row does not say who deleted it.
- Disappearing, Set By, Set At, Setting History, Missing Consistent With Timer, Missing Not Explained
- The Calls count on this page is the voice and video calls written into the message text. It is not a separate call log.
- **Icon** is the group picture. WhatsApp keeps it as `files/Avatars/<group jid>.j` (a JPEG with a `.j` name). The cell shows that picture when the extraction contains the file. A copy that only has the databases leaves the cell blank. A one-to-one chat has no icon here, because that file name is a phone address.

## Chat members

The Chat Groups summary lists who was in each conversation when the phone was acquired: group members from `group_participant_user`, and this phone from `registration_jid` in `com.whatsapp_preferences_light.xml`. When `self_user_name` is set, that username is written next to the phone on the Me entry (`Me (+number @name)`). If the preference is missing, the same username is read from `lid_display_name` on this phone's own `@lid`. A member stored as a LID is shown by contact name, then by the mapped phone JID, then by the LID. The `lid_me` slot is this phone, not an extra member. An empty group list is reported as empty. That list is not who received a particular message.

A poll's question stays in the message text. The option names from `message_poll_option` are added after it, in the order the rows were stored. When votes are stored, the text ends with the number of vote rows, for example `2 votes`. That number matches **WhatsApp Poll Votes**. The voter names are not in this report.

## Reactions

**Reactions** is on the message that was reacted to. Each reaction is the emoji, the person, and its time, for example `👍 Ada 09:15:11 29 Oct 2026 UTC`. More than one is separated by a comma, in the order they were stored. The time inside this text is always UTC: it is written as text, so it does not follow the case's time-zone setting. **Last Reaction Time** is the newest of those reactions, as a normal time column that does follow the setting.

A reaction is a row in `message_add_on` with type 56, pointing at the message by `parent_message_row_id`. The emoji is in `message_add_on_reaction`. The person is named the same way as a poll voter: this phone is the Me line, a one-to-one reaction with no sender address is the other person in the chat, and a group reaction is the sender's own address, mapped from a LID to a phone when `jid_map` has the row. The time is `message_add_on.timestamp`, the same clock as the message time.

A reaction is not a message, and it does not get its own row in this report. It still posts a notification for the chat. If you are comparing notification times with the messages, check this column before calling a notification with no message a missing message.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Emoji, person, and time | **Observed. Controlled, one reaction.** | Linked Android WhatsApp, 29 October 2026. The other person reacted 👍 to a message this phone sent. Stored as type 56 at 09:15:11, emoji in `message_add_on_reaction`. |
| A reaction posts a notification for the chat | **Observed. One reaction.** | Same test. Two posted notifications for that chat 2 seconds after the reaction. No shortcut row in People or in Usage Stats. |
| Every stored reaction is shown | **Observed.** | Same phone: 297 reactions on 215 messages. Each has an emoji and its message. Nobody had two reactions on one message. |
| Several people on one message | **Observed, not controlled.** | 38 messages on the same phone have reactions from more than one person. |
| A reaction removed later | **Not tested.** | No reaction was removed during the test. A blank emoji is left out. |

## Current location and a selected place

Both are ordinary location messages. Both store a latitude and a longitude. Reading only those two numbers makes them look the same. They are not.

**Limited testing.** One linked Android phone, one chat, 27 October 2026. The phone's GPS was set to a known public point in Oslo. Two locations were then sent from that chat.

| What was pressed | What `message_location` stored | What this report says |
|---|---|---|
| **Send your current location** | The GPS coordinates. Place name, address, and link empty. | **Current location** |
| **A place chosen from the nearby list** | Different coordinates, plus the place name, the street address, and a link. | **Selected place:** and the place name. The address and the link are their own columns. |

The selected place was not where the phone's GPS was. Treating its coordinates as "the phone was here" is wrong. **Current location** is the phone reporting its own fix. **Selected place** is a place the user chose to show.

The message row itself has no flag for this. The difference is whether the place name, address, and link are filled. This report uses that. One pair only. A pin dropped on the map with no place name was not tested, so an unnamed pin is not covered by this label.

A live location is a third action. This companion refused to start one ("Share live location on your primary phone"). A live share started on the primary phone showed in the chat as "Live Location not available". The later copy did store it: message type 16, `live_location_share_duration` 3600, and a point that matched the current-location pin rather than the selected address. The columns named final position and final time held the same point and the same time as the start. The copy was taken about two minutes into the hour.

## Voice and video calls

A WhatsApp call that was kept as a chat message is message type 90. `message_call_log` joins it to `call_log`.

| What is stored | What this report says |
|---|---|
| `video_call` 0 | **Voice call** |
| `video_call` 1 | **Video call** |
| `call_result` 5 | **Answered**. WhatsApp's own query counts this as `num_answered_outgoing_calls`. |
| `call_result` 2 | **Not answered**. Incoming rows with this result are the set WhatsApp loads for missed calls (`from_me` 0 and `call_result` 2). |
| `call_result` 4 | **result 4**. The app counts this code and does not name it. Duration was 0 on the rows already on this phone. The calls placed on 27 October 2026 did not produce a 4. |
| `call_result` 6 | **result 6**. One incoming voice row, duration 0, appeared in the same chat about ten seconds after the video call below. The screen we read did not show a third bubble. |
| `duration` | Shown after the outcome, as hours, minutes, and seconds. The stored number is a whole number of seconds. |

Sent and Received are the call's `from_me`, the same direction column as any other message. On this phone the message direction and the call-log direction matched on all 126 joined calls.

`call_log` also holds calls that have no chat message. Those rows are not in this report. On the linked phone that was 67 of 193 calls.

## Label key

The phone for the names below was a virtual device, not a handset. Android 16 (API 36), image `sdk_gphone64_arm64`, WhatsApp **2.26.42.71**. It is a linked companion, not the primary phone. The session was 27 October 2026.

Two chats were used. An existing group of three, and one one-to-one chat. Nobody was removed. The group was not renamed. In the group: a message `GTD-PIN` was sent and pinned for 24 hours, one sticker was sent, and one GIF was sent. Disappearing messages were set to 24 hours and then back to Off. In the one-to-one chat: one voice call was ended after the call screen was up, and one video call was ended before it was answered.

The other source is IPED, the open-source tool from the Brazilian Federal Police (`sepinf-inc/IPED`, file `ExtractorAndroidNew.java`, [GitHub issue 1923](https://github.com/sepinf-inc/IPED/issues/1923)). They compared the database with the words on a phone. A row marked **IPED only** was not read off this virtual phone, so this report does not use that name.

| Code | This report | What it means | Checked |
|---|---|---|---|
| Message type 13, file `video/mp4` | **GIF** | A GIF. The new one was labelled "View GIF" on screen. Two older ones in the same group show the GIPHY mark. | This VM |
| Message type 20, file `image/webp` | **Sticker** | A sticker. The new bubble was "Sent sticker". | This VM |
| Type 90, `video_call` 0, `call_result` 5, duration 3 | **Voice call, Answered, 3 sec** | The bubble said "Voice call" and "3 sec". It did not say the word Answered. That word is result 5, which WhatsApp's own SQL counts as answered. | This VM |
| Type 90, `video_call` 1, `call_result` 2, duration 0, outgoing | **Video call, Not answered** | The bubble said "Video call" and "No answer". Hanging up before an answer wrote result 2, not result 4. | This VM |
| `call_result` 4 | **result 4** | A third call outcome. IPED calls it refused. This session did not produce one. | IPED only |
| `call_result` 6 | **result 6** | One incoming voice row, duration 0, same chat, about ten seconds after the video call. Not on the screen we read. | Not named |
| Action 4, `is_me_joined` 1 | **You were added** | "You" is the account on this phone. The group opened on the words "You were added". | This VM |
| Action 6, `message_system_photo_change` | **Group icon changed** | The group showed "You changed this group's icon". The text column held the flag `dirty`, which is not shown. | This VM |
| Action 67 | **Encryption notice** | The notice at the start of the group, without the word "now". | This VM |
| Action 69, privacy type 1 | **Encryption notice** | The same group, later: "Messages and calls are now end-to-end encrypted." | This VM |
| Action 118 | **Message pinned** | Pinning `GTD-PIN` for 24 hours wrote action 118 and the line "You pinned a message". There is no subtype table. An older action 118 on this phone was not opened. It gets the same label because it is the same code. | This VM, for the new row |
| Action 5 | **Left the group** | An older group that held only this row showed a phone number and the word "left", plus the encryption notice. The number is not copied into the report. All nine action 5 rows on this phone are `is_me_joined` 0. Nobody was asked to leave the family group. | This VM |
| Action 14 | **Removed from the group** | In Minecraft lovers this phone removed a member. The line was "You removed" and the member's name. IPED lists actions 7 and 14 as removed. This phone has action 14 only. Action 7 has no row here. | This VM |
| Message type 1, file `image/jpeg` | **Photo** | The picture in Minecraft lovers was "Enlarge photo" and "Forward image". A blank type 1 row says Photo. A caption is kept. One older type 1 row has no file. | This VM |
| Action 11, empty text | **You created this group** | Two groups this phone created, shown as "Group · Only you", had that sentence. | This VM |
| Action 11 with stored text | **Unknown system action 11**, then the stored name | The family-group row stores the group name. The top of that chat showed "You were added" and no created line, so a named action 11 is not labelled as created. | This VM |
| Action 15 | **You're now an admin** | Two older groups that held only this row showed that sentence. | This VM |
| Action 69, privacy type 2 | **Business secure service** | A chat whose newest row is this code showed "This business is now using a secure service from Meta to manage this chat." | This VM |
| Action 56, `setting_duration` 86400 | **Message timer updated, 24 hours** | In a group that contains only this phone, the timer was set to 24 hours. The line was "You updated the message timer. New messages will disappear from this chat 24 hours after they're sent, except when kept." | This VM |
| Action 56, `setting_duration` 0 | **Disappearing messages off** | The same group was set back to Off. The line was "You turned off disappearing messages." The group's timer column ended at 0. An older action 56 row also stores duration 0 and gets the same label. | This VM, for the new row |
| Action 56, `setting_duration` 7776000 | **Message timer updated, 90 days** | One older row stores 7,776,000 seconds, which is the 90-day choice this phone offers. That older row was not opened. The label uses the same action as the 24-hour test, plus the stored duration. | Duration column |
| Message type 36, `setting_duration` 7776000 | **Message timer updated, 90 days** | A one-to-one chat whose only message is this type showed "Disappearing messages on" and "You updated the message timer. New messages will disappear from this chat 90 days after they're sent, except when kept." The stored duration matches the chat timer. The other type 36 rows are 24 hours and 7 days and match their chats the same way. The group timer test wrote action 56, not type 36. | This VM, for the 90-day row |
| Block, number change, username change | The words in the [system table](#system-messages) below | Taken from WhatsApp's own subtype table and its flag column. Not re-enacted on 27 October 2026. | This database |

The first disappearing-message toggle was in the family group, where this phone is not an admin. It left the timer Off and wrote no message. The toggle that wrote action 56 was in a group that contains only this phone. It did not write message type 36. Opening the older one-message groups also wrote a fresh action 69, privacy type 1, which is the encryption notice already on those screens.

A stored name on an unknown system row is kept after the unknown label. The words `true`, `false`, and `dirty` are flags, not the notice, and they are not shown.

Message type 27 leaves the message text column blank. The wording is in `message_template`: the content, then the footer when one is stored, then each button label from `message_template_button` in stored order. The report shows that text. The bubble was not opened on the phone.

## Seen on this phone, still unknown

A blank message of one of these codes now says **Unknown message type** and the number, or **Unknown system action** and the number. A row that already has text keeps that text. The code is still listed here. Counts are this companion on 27 October 2026, after the group and call tests.

| Code | Rows | Where else a name was offered | Used here |
|---|---|---|---|
| Message type 2 | 61, all blank | IPED: audio. The file is `audio/ogg` (opus). The bubble was not opened. | No |
| Message type 3 | 48, 39 blank | IPED: video. | No |
| Message type 9 | 21, 1 blank | IPED: document. | No |
| Message type 14 | 2, both have text | IPED: contact. The card is in `message_vcard`. | No |
| Message type 45 | 3, all blank | IPED: a message with buttons or a title (`message_ui_elements`). | No |
| Message type 46 | 1, has text | IPED: a reply to that kind of message. | No |
| Message type 55 | 2, all blank | IPED: same button or title family as type 45. | No |
| Message type 62 | 11, all have text | Not in the IPED list read for this. The row has a media file and a UI element. | No |
| Message type 95 | 8, all blank | No name found. `message_fixed_content_placeholder` type 0 on all 8. | No |
| Message type 99 | 21, all blank | IPED: message association. Baileys describes an album on the wire protocol, which is a different numbering. `message_album` stores image and video counts. | No |
| Message type 116 | 6, all blank | IPED: nothing is shown in the app. | No |
| Message type 118 | 1, blank | Not the pin. The pin is system action 118 on a type 7 row. No name found for message type 118. | No |
| Message type empty | 1 | No name found. | The row says **Unknown message** |
| System action 4, `is_me_joined` 0 | 2 | IPED: someone was added. The rows with `is_me_joined` 1 were opened and say **You were added**. Searching that chat for "added" did not show a system line. | No |
| System action 11 with stored text | 2 | IPED: group created. Both rows store the group name. The family chat did not show a created line. Minecraft lovers did show "You created this group", but the row has the same shape, so it is not labelled from that sentence. Empty action 11 rows are **You created this group**. | No. The stored name is kept after **Unknown system action 11** |
| `call_result` 4 | 7 calls | IPED: refused. The app counts the code and does not name it. The hang-up test wrote result 2, not 4. | No. The report says **result 4** |
| `call_result` 6 | 1 call | No name found. Incoming voice, duration 0, not on the screen we read. | No. The report says **result 6** |

## Where the other names come from

WhatsApp does not publish these database numbers. The [Cloud API message types](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages/) are words a business sends (`text`, `image`, `sticker`, `interactive`). They are not `message.message_type` and not `message_system.action_type`.

Baileys and whatsmeow, the libraries other apps use to speak WhatsApp's protocol, ship WhatsApp's wire protobuf. Its `StubType` list does name group events. The numbers are a different list. In that enum, 118 is `E2E_IDENTITY_UNAVAILABLE`. On this phone, action 118 is the pin created in the test. Those wire numbers are not applied here.

IPED (`ExtractorAndroidNew.java`, Brazilian Federal Police) is the source that maps these database integers. A name from that file is in the report only when this virtual phone showed the same words.

## System messages

Message type 7 is a system row. `message_system.action_type` is a number.

| Where it is stored | Report text |
|---|---|
| `message_system_initial_privacy_provider`, and action 69 when privacy type is 1 | Encryption notice |
| `message_system_block_contact` | Contact blocked, or Contact unblocked |
| `message_system_number_change` | Phone number changed |
| `message_system_username_change` | Username changed |
| `message_system_photo_change` | Group icon changed |
| Action 118 | Message pinned |
| Action 4 with `is_me_joined` 1 | You were added |
| Action 5 | Left the group |
| Action 11 with no stored text | You created this group |
| Action 14 | Removed from the group |
| Action 15 | You're now an admin |
| Action 69 when privacy type is 2 | Business secure service |
| Action 56, duration 0 | Disappearing messages off |
| Action 56, duration 24 hours, 7 days, or 90 days | Message timer updated, and that duration |
| Any other system action | Unknown system action, and the number |

Photo, GIF, and sticker are labelled above. Every other message type that has no text of its own says **Unknown message type** and the number. The list is [Seen on this phone, still unknown](#seen-on-this-phone-still-unknown).

---

## Not in this report

- The last-message time kept in `chat.sort_timestamp` after a clear.
- A count of messages removed by "Clear chat".
- Status updates, and older versions of an edited message.
- Calls that are in `call_log` but have no chat message. On the copy taken before 27 October 2026 that was 67 of 193 calls.
- Who voted, and which option. The poll row shows the question, the options, and the vote count. The names are in **WhatsApp Poll Votes**.
- Whether a disappearing-message timer has removed a file. The shortest timer on this build is 24 hours. It was switched on for one chat and a file was sent. The deletion itself has not been observed.

---

## Sources

- Controlled test on one linked Android WhatsApp, 26 October 2026. Companion device. One chat cleared. Not a published dataset.
- Same companion, 27 October 2026. Android 16 (API 36), WhatsApp 2.26.42.71, virtual device `sdk_gphone64_arm64`. Group and one-to-one checks in [Label key](#label-key).
- IPED, Brazilian Federal Police, `ExtractorAndroidNew.java`. Names this phone did not show are listed under [Seen on this phone, still unknown](#seen-on-this-phone-still-unknown) and are not written on the row.
- WhatsApp Cloud API and the Baileys / whatsmeow wire enum. Checked 27 October 2026. Neither publishes these database integers. The wire enum's 118 is a different event from this phone's action 118.
- The parser's earlier casework for freed-page carving (`secure_delete = FAST`), recorded in this report's description before this notes page.
