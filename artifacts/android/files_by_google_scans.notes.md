# Files by Google – Background Scans: read this first

This report lists the **scans Files by Google ran over shared storage**, with start and end times and the number of items updated. **A scan runs on its own when new files arrive; it does not show the app was opened.**

**Duration** is worked out from start and end. Scan Type and Result are shown as stored. Their meaning was not traced.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Scans happen without the user** | In the lab, a scan ran 6 to 14 seconds after each download while the app was never opened. | [What a row is](#what-a-row-is) |
| **Updated Items was 0 in the lab** | All 95 lab scans showed 0, including scans that added new files to the index. Do not read it as "nothing changed". | [What a row is](#what-a-row-is) |
| **Codes not decoded** | Every lab scan was type 3, result 1. On the Poco, 631 of 632 were too. | [What a row is](#what-a-row-is) |

---

## Where the data is

`/data/data/com.google.android.apps.nbu.files/databases/files_master_database`, table `scan_log_table` (`id`, `start_timestamp`, `end_timestamp`, `updated_items_count`, `scan_type`, `scan_result`).

## What a row is

In the lab run of 2026-11-13, every download was followed by a scan 6 to 14 seconds later, and the file appeared in *Indexed Files* at the same time. The first batch had one scan per download (14 for 14). Later batches had more scans than downloads: 28 for 14, and 43 for 24. Scans took 8 ms to 1.5 s. A scan's time is when Files by Google noticed new files, not when anyone used the app.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| A scan after each download, app not opened | **Observed. Controlled.** | Lab run `files-app-usage-001` |
| Report output | **Observed.** | Lab 95 rows, Pixel 7a 159, Poco X7 Pro 632 |
| Meaning of the type and result codes | **Not tested.** | |

## Sources

- Lab emulator `gtlab-alice`, Android 16, Files by Google `1.13157.1011540316.0-release`, run `files-app-usage-001`, 2026-11-13.
- Pixel 7a (Android 14) and Poco X7 Pro (Android 15).
