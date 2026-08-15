# Josh Hickman Android 14 test image — ground truth

Source: `Android14-ImageCreation.pdf` (thebinaryhick.blog), documenting exactly
what actions were performed on the device that produced the extraction at
`/Users/klastveita/script/case_data/Android 14 JoshHickman/`.

This is **real ground truth**, independent of any parser (human or AI) — use
it to check parser *output* against what actually happened, not just to diff
two parsers against each other.

## Scope

Transcribed: device/procedure metadata, and every non-stock and stock app's
full documented action log (install info + timestamped action/message rows).
This is the part relevant to validating artifact parsers.

**Deliberately omitted** (present in the source PDF, not transcribed here —
not useful for message/content-parser validation): the Power Events,
Wi-Fi Access Points, Bluetooth Paired Devices, and Device Lock/Unlock tables
(hundreds of rows each, relevant to device-state/timeline artifacts, not
message parsers). Pull those from the PDF directly if a future artifact
needs them.

## Files

- `device_metadata.json` — phone/Android version info, Google account,
  procedure, image creation hashes.
- `apps_non_stock_part1.json` .. `part13.json` — all 51 documented non-stock
  (Play Store) apps, alphabetical, ~4-5 apps per file to keep each small.
  `part12.json` contains **WhatsApp** — see its `note2` field: the documented
  log is a *subset* of what's in `msgstore.db`; the real database also has
  organic background/spam-group data not in this ground truth.
- `apps_stock_part1.json`, `part2.json` — the 14 stock Android apps (Camera,
  Chrome, Messages, Phone, Photos, Maps, etc.). A few very long tables
  (Camera's ~90 photo timestamps, Play Store's ~50 search queries) are
  truncated to first/last entries with a `note2` pointing at the PDF page
  range for the full list.

## Format

Each app is a JSON object keyed by app name:
```json
{
  "version": "...", "install_date": "yyyy-mm-dd", "install_time": "hh:mm",
  "username": "...", "note": "...",
  "actions": [{"date": "yyyy-mm-dd", "time": "hh:mm", "action": "...", "message": "..."}]
}
```
`date` carries forward from the previous row when the source table left it
blank (a continuation of the same day). All times are as documented: UTC-0500
before 2024-03-10 12:00 UTC, UTC-0400 (EDT) after.
