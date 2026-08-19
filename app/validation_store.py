"""validation_store.py — cross-case parser validation baselines.

Each artifact parser is built and checked against one specific GTD-
documented test image (a known OS + app version). This store records that
snapshot once per parser (see app/parser_validation.py for what's actually
captured — a SQLite schema dump plus a generalized folder-structure
fingerprint, never a raw per-device file dump) so every later run of the
same parser — against real casework, not just the GTD image — can be
diffed against it and any drift surfaced as a report next to the parser's
normal results, rather than silently trusted forever.

Keyed by "{platform}:{script_name}" — ios:whatsapp and android:whatsapp are
different apps that happen to share a filename (artifact_runner.py's
script_name is just the .py stem), so platform must be part of the key or
the two would silently collide.

Global JSON file, shared across all cases, hand-editable — same
dev-vs-frozen path convention as research_store.py (next to the exe when
frozen, else config/ beside the source), and the same load/write shape.
"""

import json
import os
import sys
from datetime import datetime, timezone


def store_path() -> str:
    """Location of parser_validation.json (next to exe when frozen, else config/)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "parser_validation.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "parser_validation.json")


def load() -> dict:
    path = store_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get(key: str) -> dict | None:
    return load().get(key)


def _write(data: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def save_baseline(key: str, snapshot: dict, source_case_label: str,
                  device_os: str) -> None:
    """Record (overwriting any prior baseline for this key) — the deliberate
    "this run is the reference" action; never called automatically."""
    data = load()
    data[key] = {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_case_label": source_case_label,
        "device_os": device_os or "",
        "schema": snapshot.get("schema", {}),
        "structure": snapshot.get("structure", []),
    }
    _write(data)


def remove(key: str) -> None:
    data = load()
    if key in data:
        del data[key]
        _write(data)
