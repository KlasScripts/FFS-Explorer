# Recent Message Notifications (Experimental): read this first

**Status: experimental, lead only.** This is weak evidence on its own. Use it to point you at a chat and a time to check, never as proof that a message arrived or was deleted.

This report lists notifications Android posted for a conversation, a one-to-one chat or a group. One row is one posted notification. **Time** is when it was posted. **App** is the Android package. **Conversation id** is the shortcut id the app stored for that chat.

The row does not say the notification was on screen, and it does not say anyone opened the chat.

**What it is good for.** A row stays after the message behind it has been deleted. A row on its own, with no matching message left in the app, is a **tentative lead** that something may have come into that chat at that time. It is not proof of a message: a reaction posts the same way, and some apps do not keep every message they notify for. See [Using a single row as a lead](#using-a-single-row-as-a-lead).

**What gets in the way.** Apps sometimes post again for many chats at once, a sweep. On the images checked, sweeps far outnumber real notifications. They usually follow the first unlock after a boot, but they can happen at other times too. A sweep row looks the same as a real one, and a real notification can land inside a sweep. See [Sweeps](#sweeps).

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Sweeps hide the real notifications** | An app re-posts many chats in the same second. Those rows are not new messages, and they can fill the report. They are marked, not removed. | [Sweeps](#sweeps) |
| **A single row is a lead, not proof** | It can be a deleted incoming message. It can also be a reaction, or a message the app never stored. | [Using a single row as a lead](#using-a-single-row-as-a-lead) |
| **Two stored posts a few milliseconds apart are one notification** | The app posts, then updates the same notification. The report keeps the later time. | [One row](#one-row) |
| **Opening a chat leaves no row** | A shortcut use with no posted notification is not listed. The chat can still have been opened. | [What is left out](#what-is-left-out) |
| **Posted is not seen or read** | The time is when Android accepted the notification. | [What the time is](#what-the-time-is) |
| **The file does not keep the older posts** | On current Android the recent file drops events after about four hours. An older image can hold longer. | [How long a row lasts](#how-long-a-row-lasts) |
| **Messages held while the phone was off arrive just after the first unlock** | They land in the same minute as the sweep, and their notification can come 20 to 40 seconds after the message is stored. | [Sweeps](#sweeps) |
| **A reaction posts like a message** | A row can be a reaction to an older message. Check the app's own reactions before calling a row with no message a missing message. | [Using a single row as a lead](#using-a-single-row-as-a-lead) |

---

## Using a single row as a lead

A row that is not part of a sweep, one chat on its own, may be a real incoming notification for that conversation. Deleting the message afterwards does not remove the row. So when the app no longer has a message for that chat at that time, the row is a tentative lead that something possibly came in and is no longer stored.

Before treating it as a lead:

1. **Rule out a sweep.** Chats Same Second should be 1, and Seconds After First Unlock blank. If the row is just after a first unlock, it can still be real: see [Sweeps](#sweeps).
2. **Look for the message in the app.** Search that conversation's incoming messages from about 40 seconds before the row up to the row itself.
3. **Look for a reaction** on any message in that chat at about the same time. A reaction posts exactly like a message.
4. **Read Usage Stats around the time.** A shortcut row with the same conversation id within a few milliseconds, and an alert on the app's chat channel about a second later, is what an incoming message looked like in testing. A reaction had the alert and no shortcut row.
5. **Consider what the app keeps.** Some apps notify for chats whose messages they do not store, or only store once the chat is opened. A channel or a large group is the usual case.

What the row cannot give you: the text, who sent it in a group, or whether it was a message at all. It says that the app posted a notification for that conversation at that time.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| A row stays after the message is deleted | **Observed. Controlled, two messages.** | Linked Android WhatsApp, 29 October 2026. Two incoming one-to-one messages, each deleted with Delete for me about a minute later. Both rows stayed. Neither message was left in the database. Usage Stats had a shortcut row with that chat's id at the same millisecond, and an alert about a second later. |
| A reaction looks the same | **Observed. One reaction.** | Same test. A 👍 reaction posted two rows for the chat, 2 seconds after it was stored. Usage Stats had the alert and no shortcut row. |
| Single rows on images that were not tested | **Observed.** | Pixel 3: all 6 Messages rows have a stored incoming message. Poco X7 Pro: all 14 Messages rows and the WhatsApp row do. Telegram: 23 of 35 rows do. |
| Rows with no stored message, not explained | **Observed, not resolved.** | Poco X7 Pro, Telegram. 10 rows belong to one channel or supergroup whose newest stored message is weeks older, which fits Telegram not keeping that channel's messages. 2 rows are private chats with no gap in Telegram's message numbering around them, so they are not a stored message that was deleted. |
| Group chats | **Not tested.** | The deleted-message test was one-to-one only. |

---

## One row

People writes type 2, notification posted, in `people/<package>/event/shortcut/<id>/events/recent`. The time is the notification's post time, in Unix milliseconds.

WhatsApp on a linked phone, 28 October 2026, stored two posted times for each incoming message, 1–5 ms apart. This report keeps the later one. Two posts for the same conversation that are a second or more apart stay as two rows. On the Pixel 3 image, Messages chat 1 has posts at 14:47:05.122 and 14:47:06.713, and both remain.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Two posts a few milliseconds apart become one row, the later time | **Observed. Controlled.** | Linked Android WhatsApp, 28 October 2026. Four incoming messages. Each was stored twice. The later time matches the conversation's last-event time for the newest one. |
| Posts more than a second apart stay separate | **Observed.** | Pixel 3 image, 16 December 2021. Messages chat 1, the two times above. |

---

## What the time is

The time is `getPostTime()` from `onNotificationPosted`. It is the moment the app handed the notification to Android.

A notification can be posted and still stay hidden, by Do Not Disturb, a low-importance channel, or because that chat is already on screen. Seen and interruption counts are in **Usage Stats**. A tap on the notification is a different People event and is not a row here.

A row is a notification for the chat, not always a message. A reaction to an older message posts the same way. A missed WhatsApp call did not leave a row.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| The row is the post, not a tap and not time on screen | **Observed. Controlled.** | Same linked phone, 28 October 2026. Three of the four messages were opened, one from the notification and one from the chat list. None of those opens is a row. The message left unread is the last row. |
| A missed call does not post a row | **Observed. One call.** | Linked Android WhatsApp, 29 October 2026. A missed voice call posted nothing to this file. |

---

## What is left out

A shortcut row is written when the app reports the conversation shortcut as used. An incoming message on the Pixel 3 image does that a few milliseconds before the post. Opening the chat, sending from it, or picking it in the share sheet can write a shortcut row with no post beside it. Those rows are not in this report.

Messages chat 17 on the Pixel 3 image has a shortcut time on 11 December 14:46:01.959 and no posted notification in the recent file. It has no row here. The conversation row still has a last-event time of 9 December 02:49:41, which is older than the recent file.

A share to a screen is stored under `event/class/`, with the activity class as the id. The Gmail compose screen on that image, `com.google.android.gm.ComposeActivityGmailExternal`, is that kind of row. It is not listed. It is not a conversation notification.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Shortcut-only use is absent | **Observed.** | Pixel 3, Messages chat 17, as above. |
| A share to a screen is absent | **Observed.** | Same image. One class event, an image share to Gmail compose. Zero rows for that id. |

---

## Sweeps

A sweep is an app posting again for many of its chats at once, usually every chat it still has a notification showing for. No new message is behind those rows. They get in the way: they can outnumber the real notifications, and they look exactly like them. On the Pixel 7a image every row in this report is part of a sweep.

**Most sweeps follow the first unlock after a boot.** After a phone boots, nothing in the user's own storage can be read until the first unlock. From then on, apps post again for their chats.

**They can happen at other times too.** On the Pixel 7a image, Usage Stats shows WhatsApp alerting for 5 chats in the same second on 26 July 2024, 15:34:27, with no boot near it and no message stored for any of them. An app update, a sync, or the app restarting may cause this. Which one it was is not known.

**Real notifications can land inside a sweep.** Messages held by the network while the phone was off are delivered in the same minute as the sweep after the first unlock.

Two columns mark these rows:

- **Seconds After First Unlock**: how long after the last first unlock in Usage Stats (`USER_UNLOCKED`) the row was posted, when that is a minute or less. Blank when it is longer, or when the extraction has no Usage Stats.
- **Chats Same Second**: how many different chats of the same app were posted within a second of the row, counting the row itself.

The column uses the unlock, not `DEVICE_STARTUP`. On the Galaxy A53 image one boot has an unlock row and no startup row.

Neither column removes a row, and neither one says whether a message is behind it. Check the app's messages for that chat. A message delivered after power-on can be stored 20 to 40 seconds **before** its notification, so look back further than a few seconds.

- A large Chats Same Second: a sweep, whether or not it follows an unlock.
- A small number right after an unlock: either a small sweep, or a message delivered after power-on. Only the app's messages tell the two apart.
- Chats Same Second of 1 and no unlock time: not a sweep on these markers. See [Using a single row as a lead](#using-a-single-row-as-a-lead).
- A sweep can be spread over more than a second. Two chats 2.6 seconds apart each show 1. Look at the rows on either side.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| A sweep of existing notifications | **Observed. Two boots.** | Pixel 7a image, 28 July 2024. Each first unlock was followed 20 to 23 seconds later by posts for the same 22 Messages chats, all within under a second. No message was received at either time. Each of those chats had an unread incoming message. |
| Messages delivered after power-on | **Observed. One boot, two messages.** | Galaxy A53 image, 22 December 2025. The phone had been off since 16 December. First unlock at 16:43:31. Two messages sent while it was off (19 and 21 December) were stored at 16:43:38 and 16:43:47. Their notifications posted at 16:44:09 to 16:44:24, 38 to 54 seconds after the unlock. |
| A small sweep | **Observed. One boot.** | Same image. Second first unlock at 16:53:45. The same two chats were posted again at 16:54:34 and 16:54:36, 2.6 seconds apart, so each shows Chats Same Second 1. No new message. |
| A sweep with no boot | **Observed in Usage Stats only.** | Pixel 7a image, WhatsApp, 26 July 2024 15:34:27. Five chats alerted in the same second, no message behind any of them. That time was outside this file's four hours, so it is not a row here. |
| First unlock time | **Observed.** | `USER_UNLOCKED` in Usage Stats, from the same extraction. |

## How long a row lasts

On Android 16 the recent file drops events older than four hours. The conversation row can still show an older last-event time after the recent file has dropped the event. The Pixel 3 image, taken in 2021, still held posts from 11 December through 16 December.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Four-hour drop on current Android | **From Android 16 source.** | `MAX_EVENTS_AGE` is four hours. Not re-checked by waiting four hours on a phone. |
| An older image can hold several days | **Observed.** | Pixel 3 image, posts dated 11–16 December 2021. |

---

## Not in this report

- Message text, sender, and channel.
- Whether the notification was shown or opened.
- Shortcut uses, in-app time on screen, and shares to a screen.
- The conversation list's last-event time. That time is not a column in a chat report.

## Sources

- Android 16 People `Event` types and `NotificationListener.onNotificationPosted`.
- Pixel 3 full-file image, 16 December 2021, `people/*/event/*/events/recent`. 11 posted notifications after the one-second merge. No class row.
- Linked Android WhatsApp, 28 October 2026. Four incoming messages, the last one left unread.
- Same phone, 29 October 2026. Two incoming messages deleted, one reaction, one missed call, extracted after a clean shutdown.
- Pixel 7a, Galaxy A53, and Poco X7 Pro full-file images, checked against each app's own messages on 29 October 2026.
