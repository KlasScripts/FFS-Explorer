# ios-ffs-browser

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![PySide6](https://img.shields.io/badge/UI-PySide6-green.svg)](https://pypi.org/project/PySide6/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A forensic-grade desktop application for browsing **iOS & Android Full File System (FFS)** extractions. This tool allows you to explore images from **GrayKey** and **Cellebrite UFED** directly from the ZIP archive with no unpacking required.

The browser reconstructs the original file system using vendor-specific metadata, resolves iOS app container identifiers, and provides a UI optimized for forensic triage.

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
*   **Vendor-Aware Reconstruction:** 
    *   Parses Cellebrite `.msgpack` metadata.
    *   Parses GrayKey ZIP extra-field metadata.
*   **iOS App Container Resolution:** Automatically maps cryptic UUIDs to human-readable Bundle IDs.
*   **Forensic Artefact Shortcuts:** Quick-jump to high-value files:
    *   `KnowledgeC`, `Biome`, `SMS`, `Safari`, `Keychain`.
    *   Android-equivalent database paths.
*   **Integrated Hex Viewer:** Preview the first 64 KB of any file instantly.
*   **Export & Audit:** Recursive exporting of directories and basic audit logging.

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
4.  **UI Layer:** A PySide6-based interface handles the tree rendering and hex previews.

---

## 📁 Roadmap

- [ ] Additional Android artefact shortcuts.
- [ ] Timeline visualization view.
- [ ] Integrated SQLite database previewer.

---

## 🤝 Contributing

This tool is designed for the DF community. Pull requests for new vendor formats or artefact shortcuts are welcome.
