# Josh Hickman iOS 17 test image — ground truth

Source: `iOS17-ImageCreation.pdf` (thebinaryhick.blog), documenting exactly
what actions were performed on the device that produced the extraction at
`/Users/klastveita/script/test data/IOS17 JoshHickman/EXTRACTION_FFS.zip`.

This is **real ground truth**, independent of any parser (human or AI) — use
it to check parser *output* against what actually happened, not just to diff
two parsers against each other. Same purpose and conventions as
[[../josh_hickman_android14]], adapted for iOS's app-metadata shape.

## Scope

Transcribed: device/procedure metadata, and every non-stock and stock app's
full documented action log (install/update info + timestamped action/message
rows).

**Deliberately omitted** (present in the source PDF, not transcribed here —
not useful for message/content-parser validation, same rationale as the
Android doc): the Power Events, Wi-Fi Access Points, Bluetooth Paired
Devices, and Device Lock/Unlock tables (hundreds of rows each), plus two
iOS-specific sections not present in the Android doc: Other Events (a
device-timeline log — screenshots, AirDrop, backup/restore, connectivity
toggles — not app content) and Screen Layout (a single home-screen
screenshot figure, no transcribable data). Pull those from the PDF directly
(pages 102-127) if a future artifact needs them.

**Source discrepancy, not a transcription error:** the PDF's own procedure
text says "Fifty-five (55) third-party apps were installed," but the
document's own per-app listing contains 56 distinct apps, each with its own
full `Name:`/`Version Number:`/install-date metadata block and action table
(verified structurally — none of the 56 are a mis-parsed contact sub-block).
Transcribed as documented; the "55" is the source's own summary count, not
corrected here.

## Files

- `device_metadata.json` — phone/iOS version info, procedure, Apple account,
  the (multi-period, more complex than Android's single cutover) timezone
  table, and image-creation hashes.
- `apps_non_stock_part1.json` .. `part14.json` — all 56 documented non-stock
  (App Store) apps, in the PDF's own order, ~4 apps per file to keep each
  small.
- `apps_stock_part1.json` .. `part4.json` — the 20 stock iOS apps (AppStore,
  Calendar, Camera, CarPlay, Clock, FaceTime, Files, Find My, Fitness,
  Health, Journal, Mail, Maps, Messages, Music, Notes, Phone, Photos,
  Safari, Weather). Camera's ~129 photo-timestamp rows are truncated to
  first/last 10 with a `note2` pointing at the PDF page range for the full
  list (same convention as Android's Play Store search-query truncation).

## Format

Same base shape as the Android ground truth, extended for iOS-specific
content this document has that Android's didn't:

```json
{
  "AppName": {
    "version": "...", "install_date": "yyyy-mm-dd", "install_time": "hh:mm",
    "updated_version": "...", "update_date": "yyyy-mm-dd", "update_time": "hh:mm",
    "username": "...", "own_phone_number": "...",
    "contact_name": "...", "contact_email": "...", "contact_phone": "...",
    "note": "...", "note2": "...", "transcription_note": "...",
    "actions": [{"date": "yyyy-mm-dd", "time": "hh:mm", "action": "...", "message": "..."}],
    "workouts": [{"...": "..."}], "daily_activity_metrics": [{"...": "..."}]
  }
}
```

Every field is optional and omitted (not present with an empty value) when
the source doesn't have it. `date` carries forward from the previous action
row when the source table left it blank (a continuation of the same day),
already resolved in this JSON — never blank/null here.

**Fields beyond the Android baseline, and why:**
- `contact_name` / `contact_email` / `contact_phone` — several apps
  (Snapchat, Telegram, Wire, TeleGuard, ...) document a second `Name:` block
  directly under the app's own metadata, for the chat contact/peer used in
  that app's testing (not a second app). Captured here rather than folded
  into `note`, since it's structured, citable information (a phone number
  or email an examiner may want to search for elsewhere in the case).
- `own_phone_number` — a few apps (Viber, WhatsApp, Wickr Pro) document the
  device's *own* registered number for that app specifically (distinct from
  the shared device number in `device_metadata.json`, though it matched on
  this image). Kept separate from `contact_phone` so "whose number is this"
  is never ambiguous from the field name alone.
- `note2` — reserved for "table truncated, see PDF pages X-Y" pointers, same
  convention as the Android doc (Camera here; Play Store search history
  there).
- `transcription_note` — flags a source-document oddity transcribed
  verbatim rather than silently corrected: e.g. a garbled date
  ("20224-04-28"), a semicolon-typo time ("11;15" / "14;59"), a table row
  with a blank date/time carried forward from the row above, or an
  Install-Date that doesn't match the first action row's own date (Wickr
  Pro). None of these are transcription mistakes on this side — check the
  PDF page cited (or the surrounding rows) before treating the value as
  wrong.
- `workouts` / `daily_activity_metrics` — FitBit and Garmin Connect each
  document two extra source tables (workout summaries; daily step/sleep/
  floor metrics) that don't fit the Date/Time/Action/Message shape at all.
  Rather than force-fit or drop this real data, each is its own array of
  column-keyed objects, separate from `actions`.
- An action row occasionally carries a `subject` key alongside `message`
  (Gmail, Proton Mail, Tutanota) when the source table has a distinct
  Subject column in addition to a body/message column.

## Notes on interpreting `message` text

Per the source PDF's own convention (stated on page 1, preserved as-is
rather than reformatted): transferred media (pictures/videos) and emojis
are described in the Message column in *(italics/parentheses)* — this shows
up throughout as ordinary parenthetical text inside `message`, e.g.
`"(Bob Barker)"` for a sent picture's caption/description, or `"(Smiley
face emoji)"` inline in a text message. This is the source's own
description of the media/emoji, not a literal caption stored by the app.

## Timezone

All times are as documented — see `device_metadata.json`'s `timezone_note`
for the full multi-period breakdown (this device changed timezone twice
more than the Android image did, including a brief UTC+0100/CET period
2023-11-22 through 2023-11-28). Any data dated before 2024-01-23 11:16
occurred while the device was still on iOS 16.1.2 (also noted in
`device_metadata.json`).
