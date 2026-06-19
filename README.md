# FFS Explorer

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![PySide6](https://img.shields.io/badge/UI-PySide6-green.svg)](https://pypi.org/project/PySide6/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A desktop application for exploring **iOS & Android Full File System (FFS)** extractions — the kind produced by tools such as **GrayKey** and **Cellebrite UFED** — directly from the ZIP archive with no unpacking required.

It reconstructs the original file system from vendor-specific metadata, resolves iOS app-container identifiers, and provides previews (hex, text, and SQLite) for inspecting individual files.

The project is built as a **learning, understanding, and validation** aid: a way to see how these extractions are structured, how vendor metadata is stored, and how on-disk artefacts such as SQLite databases (including their write-ahead logs) are laid out — and to cross-check what mainstream forensic suites report against the raw data.

---

## ⚠️ Purpose & Disclaimer

> **Disclaimer:** This project is intended for educational and research purposes using publicly available data structures. It does not represent operational practices or capabilities of any law enforcement organisation.
>
> **Note:** This tool has not been validated for evidential or casework use.

It is intended for learning about FFS data structures and for sanity-checking / validating the behaviour of mainstream forensic tools — not as a replacement for them, and not for producing evidential results.

---

## 📦 Supported Extraction Formats

Unlike standard archive utilities, this browser implements custom parsers to handle how forensic vendors store metadata.

### 1. Cellebrite UFED (.zip)
Cellebrite packages FFS extractions using a split structure where the ZIP entries themselves lack standard metadata:
*   `filesystem2/`: Raw file system tree.
*   `metadata2/metadata.msgpack`: MessagePack dictionary containing `ctime`, `mtime`, and `size`.
*   **Handling:** All metadata is reconstructed by mapping the msgpack sidecar to the file tree.

### 2. GrayKey (`full_files` .zip)
GrayKey uses a different approach, storing critical file metadata within **ZIP extra fields** rather than the central directory.
*   **Handling:** The browser manually parses GrayKey ZIP extra fields to normalize timestamps and file attributes, ensuring a consistent tree structure across vendors.

---

## 🧠 Key Features

*   **Zero-Extraction Browsing:** Loads and navigates ZIPs in streaming mode to save disk space and time.
*   **Vendor-Aware Reconstruction:** Parses Cellebrite `.msgpack` metadata and GrayKey ZIP extra-field metadata.
*   **iOS App Container Resolution:** Automatically maps cryptic UUIDs to human-readable Bundle IDs.
*   **Artefact Shortcuts:** Quick-jump to high-value paths (`KnowledgeC`, `Biome`, `SMS`, `Safari`, `Keychain`, and Android equivalents).
*   **Multi-Pane File Preview:**
    *   **Hex** — lazily paginated, so even large files open instantly.
    *   **Text** — pretty-prints JSON, XML, and plist (binary plist included).
    *   **Database** — full SQLite browser (see below).
*   **Media Browser:** Thumbnail grid for images and video, with frame extraction.
*   **Keyword Search:** Search across the extraction, including inside nested archives.
*   **Artefact Viewer:** Runs bundled parsers (e.g. `Photos.sqlite`, SMS, WhatsApp) and shows results in a sortable, filterable table.
*   **Export & Audit:** Recursive directory export, data-integrity checks, and basic audit logging.

---

## 🗄️ SQLite Database Browser

Databases are detected by **content** — the 16-byte `SQLite format 3` header — not by file extension, so databases with unusual or missing extensions (common in iOS extractions) are still recognised, and non-database files that merely end in `.db` are ignored.

*   **Schema at a glance:** lists every table with its row count.
*   **Browse rows** in a lazily paged grid that stays responsive on large tables, with a per-table text filter and **CSV export**.
*   **WAL-aware:** when a `-wal` sidecar is present it is applied so you see the current state. The table is shown as a unified **net-change view** — rows the WAL **added / deleted / modified** are highlighted in place, with modified cells rendered as `old → new`.
*   **"WAL updates only" toggle:** hide unchanged rows to focus on what the WAL changed, with the table dropdown narrowed to just the tables the WAL touched (each annotated with its update count).

This makes it easy to *understand* how SQLite stores a table and its recent (WAL) activity, and to *validate* how a commercial tool presents the same database.

> **Scope note:** the WAL view is a **net-change** comparison (last-checkpointed state vs. WAL-applied state). It is not a frame-by-frame WAL carve or a deleted-record recovery engine.

---

## 🔍 Why metadata parsing matters

Standard ZIP libraries often fail to reconstruct forensic images correctly because vendors strip metadata from the standard headers:

| Feature | Standard ZIP Utility | ios-ffs-browser |
| :--- | :--- | :--- |
| **Timestamps** | Often missing/incorrect | **Accurate (Parsed)** |
| **File Sizes** | May report 0 bytes | **Corrected** |
| **App Names** | UUIDs only (`A4B2...`) | **Mapped to Bundle IDs** |
| **Structure** | Flat or fragmented | **Logical Tree** |

---

## 🛠️ Internal Architecture

1.  **Stream Loader:** Accesses the ZIP without full decompression.
2.  **Tree Builder:** Constructs an in-memory representation of folders and files.
3.  **Metadata Overlay:** Merges vendor-specific metadata (msgpack/extra-fields) onto the tree nodes.
4.  **Preview Layer:** Renders hex, decoded text, and — for SQLite files — a paged table browser with the WAL net-change view.
5.  **UI Layer:** A PySide6-based interface ties the tree, previews, media, search, and artefact views together.

---

## 📁 Roadmap

- [ ] Additional Android artefact shortcuts.
- [ ] Timeline visualization view.

---

## 🤝 Contributing

This tool is shared with the DF learning community. Pull requests for new vendor formats or artefact shortcuts are welcome.

---

## 📄 Licence

FFS Explorer is released under the **GNU General Public License v3.0**.

You are free to use, modify, and distribute this software. If you distribute a modified version — including bundling it in a commercial product — you must release the source code of your modifications under the same GPL v3 licence. See [LICENSE](LICENSE) for the full terms.
