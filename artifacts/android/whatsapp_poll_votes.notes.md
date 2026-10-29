# WhatsApp Poll Votes (Android): read this first

This report lists votes on polls that are still in `msgstore.db`. One row is one selected option. Filter **Question** to see one poll's result.

The poll itself stays in **WhatsApp Messages**. That row has the question (Text), the option names (Poll Options), and **Poll Votes**, the number of rows here for that poll. The names are only here.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Two options from one person are two rows** | Poll Votes counts rows, so one person who picks two options adds two. | [What a row is](#what-a-row-is) |
| **A 1:1 vote often has no sender address** | The other person's vote names the chat. It does not carry a separate sender id. | [Who the voter is](#who-the-voter-is) |
| **A blank Option is a vote with no choice stored** | The person is still listed. The option cell is empty. | [What a row is](#what-a-row-is) |

---

## What a row is

`message_add_on` type **67** is a vote. Its parent is the poll message. The chosen option is `message_add_on_poll_vote_selected_option`.

- One selected option is one row.
- A person who selects two options has two rows, same voter and time, different options.
- A vote add-on with no selected option is one row. **Option** is blank. It still counts toward the number on the poll.
- A vote whose poll message is no longer in `message` is not listed. The question would have nothing to point at.

**Time** is the vote time, not the time the poll was created.

---

## Who the voter is

| What is stored | Who **Voter** is |
|---|---|
| `from_me` is 1, or the sender server is `lid_me` | This phone. The label is the same Me line as Chat Groups (`Me (+number @name)` when both are stored). There is no sender address on this phone's own vote. |
| A 1:1 chat, and the vote has no sender id | The other person in that chat. The name is the contact on the mapped phone, then the phone, then the stored address. |
| A group, and the vote has a sender | That sender. A LID is mapped to its phone the same way as an incoming group message. The contact name is on the phone. |
| A group, and the vote has no sender id | **Voter** is left blank. The chat is not used as a stand-in. |

`lid_me` is this phone's slot in a group member list. It is not another person, and a vote stored on it is Me.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| This phone's vote, option known beforehand | **Observed. Controlled.** | One poll in a 1:1 chat, linked Android phone, 27 October 2026. The vote row is `from_me` 1, sender id −1, and the selected option is the option tapped on screen. One vote row for that poll (Poll Votes 1). |
| The other person in a 1:1 chat | **Observed only.** | Same phone, 26 October 2026. A vote on a 1:1 poll has `from_me` 0 and no sender id, and the selected option is stored. That poll was not the lab poll. The other person's vote on the lab poll was not in the copies kept after the chat was cleared. |
| A group voter | **Observed only. Not a staged poll.** | Same phone. Group votes store a sender LID, mapped to a phone, with a selected option. None of those polls was created for this test. |
| `hide_participant_names` | **Not seen set.** | Unset on the polls checked. This report shows the sender the vote row stores. |

Whether a poll allowed more than one answer is not stored in a column that matched the screen. This report does not say single-choice or multiple-choice. It lists the options that were stored.

---

## Sources

- Controlled vote on one linked Android WhatsApp, 27 October 2026. This phone voted on a poll it had just sent.
- Other votes on that same phone, read from `msgstore.db`, not staged.
