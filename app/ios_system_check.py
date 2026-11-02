"""ios_system_check.py — what an examiner may miss in an iOS case, and signs
of jailbreaking or sideloading (ASSUMPTIONS.md A-82, A-83).

The iOS system partition is not listed, processed or searched (A-82). The
main forensic reason to look there is tampering: jailbreak files, package
managers, SSH, a changed /etc/hosts. This check looks for a fixed list of
such paths anywhere in the archive (system and user partition) and reads
/etc/hosts, once per case, so those signs are not lost with the partition.

A path found is reported as a SIGN, with the exact zip entry, never as
proof: a file can be left from an earlier jailbreak, a security tool, or
the extraction itself. A path not found means only that this extraction
holds no entry of that name; how much of the system partition the
extraction holds is reported alongside (coverage), so "nothing found" in a
partial extraction is not read as "not jailbroken".

Sources of the path list (each entry names its own):
  * MASTG: OWASP MASTG-KNOW-0084 "Jailbreak Detection", the indicator
    paths listed there (fetched 2026-11-02).
  * rootless: rootless jailbreaks (Dopamine, palera1n rootless) install
    under /var/jb and /private/preboot/<hash>/jb-<id>/; not in MASTG.
  * pkg: package-manager apps and folders (Sileo, Zebra, dpkg), the same
    names iLEAPP's "FSEvents - APT & dpkg" report treats as package-
    management activity; not in MASTG.
  * sideload: TrollStore, which installs apps outside the App Store via a
    CoreTrust bug (not a jailbreak); not in MASTG.

Qt-free. Reads the archive only through the case's zip index
(zip_cd_cache), never with a raw zipfile read.
"""

from __future__ import annotations

import fnmatch
import json

LOGIC_VERSION = "1"
RUN_TYPE = "ios_system_check"

# (device path or glob, what it is, source). Device paths are absolute on
# the phone, without the leading "/", with /var, /etc and /tmp written as
# their real /private/... locations. A path matches the entry itself or
# anything under it (a folder).
INDICATORS = (
    ("Applications/Cydia.app", "Cydia package manager", "MASTG"),
    ("Applications/FakeCarrier.app", "jailbreak tweak app (FakeCarrier)", "MASTG"),
    ("Applications/Icy.app", "Icy package manager", "MASTG"),
    ("Applications/IntelliScreen.app", "jailbreak tweak app (IntelliScreen)", "MASTG"),
    ("Applications/MxTube.app", "jailbreak app (MxTube)", "MASTG"),
    ("Applications/RockApp.app", "Rock package manager", "MASTG"),
    ("Applications/SBSettings.app", "jailbreak tweak app (SBSettings)", "MASTG"),
    ("Applications/WinterBoard.app", "jailbreak theming app (WinterBoard)", "MASTG"),
    ("Library/MobileSubstrate", "MobileSubstrate tweak loader", "MASTG"),
    ("System/Library/LaunchDaemons/com.ikey.bbot.plist", "iKee worm launch daemon", "MASTG"),
    ("System/Library/LaunchDaemons/com.saurik.Cydia.Startup.plist", "Cydia launch daemon", "MASTG"),
    ("bin/bash", "bash shell (not part of iOS)", "MASTG"),
    ("bin/sh", "sh shell (not part of iOS)", "MASTG"),
    ("private/etc/apt", "APT package manager settings", "MASTG"),
    ("private/etc/ssh/sshd_config", "SSH server settings", "MASTG"),
    ("private/var/lib/apt", "APT package database", "MASTG"),
    ("private/var/lib/cydia", "Cydia data", "MASTG"),
    ("private/var/mobile/Library/SBSettings/Themes", "SBSettings themes", "MASTG"),
    ("private/var/stash", "Cydia stash folder", "MASTG"),
    ("private/var/tmp/cydia.log", "Cydia log", "MASTG"),
    ("usr/bin/sshd", "SSH server", "MASTG"),
    ("usr/libexec/sftp-server", "SFTP server", "MASTG"),
    ("usr/libexec/ssh-keysign", "SSH helper", "MASTG"),
    ("usr/sbin/sshd", "SSH server", "MASTG"),
    ("private/var/cache/apt", "APT cache", "MASTG"),
    ("usr/sbin/frida-server", "Frida instrumentation server", "MASTG"),
    ("usr/bin/cycript", "Cycript", "MASTG"),
    ("usr/local/bin/cycript", "Cycript", "MASTG"),
    ("usr/lib/libcycript.dylib", "Cycript library", "MASTG"),
    ("private/var/log/syslog", "syslog file (not kept by stock iOS)", "MASTG"),
    ("private/var/jb", "rootless jailbreak install folder", "rootless"),
    ("private/preboot/*/jb-*", "rootless jailbreak install folder", "rootless"),
    ("Applications/Sileo.app", "Sileo package manager", "pkg"),
    ("Applications/Zebra.app", "Zebra package manager", "pkg"),
    ("private/var/lib/dpkg", "dpkg package database", "pkg"),
    ("private/var/containers/Bundle/Application/*/TrollStore.app", "TrollStore (installs apps outside the App Store)", "sideload"),
)

# Folders whose presence shows how much of the system partition the
# extraction holds (a stock iPhone has all four).
COVERAGE_FOLDERS = ("System/Library", "Applications", "usr", "private/etc")

HOSTS_PATH = "private/etc/hosts"
# Lines a stock iOS /etc/hosts holds (besides comments and blank lines).
_STOCK_HOSTS = {("127.0.0.1", "localhost"), ("255.255.255.255", "broadcasthost"),
                ("::1", "localhost"), ("fe80::1%lo0", "localhost")}


def device_path(zip_name: str, adapter) -> str | None:
    """The path *zip_name* has on the phone (no leading '/'), or None for an
    entry that is not part of either partition (Cellebrite's Log.txt,
    extra/, metadataN/) or an archive format this check does not cover."""
    name = zip_name.lstrip("/").rstrip("/")
    fmt = adapter.format
    if fmt == adapter.FORMAT_GRAYKEY:
        return name
    if fmt != adapter.FORMAT_CELLEBRITE:
        return None
    user = adapter.user_prefix + "/"
    if adapter.old_layout:
        # One filesystemN/ holds both partitions, as on the phone.
        return name[len(user):] if name.startswith(user) else None
    if name.startswith(user):
        return "private/var/" + name[len(user):]
    sys_ = (adapter.sys_prefix or "") + "/"
    if adapter.sys_prefix and adapter.sys_prefix != adapter.user_prefix and name.startswith(sys_):
        return name[len(sys_):]
    return None


def _normalise(path: str) -> str:
    """/var, /etc and /tmp are links to /private/... on iOS; GrayKey and
    some tools store either spelling."""
    for short in ("var/", "etc/", "tmp/"):
        if path.startswith(short):
            return "private/" + path
    return path


def _key_len(parts: tuple) -> int:
    """How many leading folder names of a pattern are plain (no '*'), at
    most 3: the pattern is looked up by those."""
    n = 0
    while n < min(3, len(parts)) and "*" not in parts[n]:
        n += 1
    return n


# {leading folder names: [(pattern, what, source, parts)]}: each path is
# looked up by its first 1, 2 and 3 folder names, so the ~40 patterns cost
# three dict lookups per archive entry, not forty comparisons.
_BY_KEY: dict[tuple, list] = {}
for _p, _w, _s in INDICATORS:
    _parts = tuple(_p.split("/"))
    _BY_KEY.setdefault(_parts[:_key_len(_parts)], []).append((_p, _w, _s, _parts))


def _matches(parts: tuple, dev_parts: list) -> bool:
    """*dev_parts* is the pattern's path or lies under it; '*' matches
    within one folder name only."""
    return (len(dev_parts) >= len(parts)
            and all(fnmatch.fnmatchcase(d, p) for p, d in zip(parts, dev_parts)))


def _candidates(dev_parts: list):
    for k in (1, 2, 3):
        if len(dev_parts) >= k:
            yield from _BY_KEY.get(tuple(dev_parts[:k]), ())


def check(names, adapter, read_entry) -> dict | None:
    """Run the check over the archive's entry *names* (zip names, as in the
    case's zip index). *read_entry(zip_name) -> bytes* reads one entry
    (used only for /etc/hosts). Returns None for an archive that is not
    iOS (no private/var/mobile/), else a JSON-safe result:

      system_entries   entries of the system partition in this extraction
      coverage         {folder: entries under it} for COVERAGE_FOLDERS
      signs            [{path, what, source, entries, example}] found
      hosts            {zip_name, extra_lines} or None when not extracted
      checked          number of indicator paths looked for
    """
    is_system = getattr(adapter, "is_ios_system_entry", None)
    found: dict[str, dict] = {}
    coverage = dict.fromkeys(COVERAGE_FOLDERS, 0)
    system_entries = 0
    ios = False
    hosts_zip = None
    for zname in names:
        dev = device_path(zname, adapter)
        if not dev:
            continue
        dev = _normalise(dev)
        if dev.startswith("private/var/mobile/"):
            ios = True
        if is_system is not None and is_system(zname):
            system_entries += 1
        for folder in COVERAGE_FOLDERS:
            if dev == folder or dev.startswith(folder + "/"):
                coverage[folder] += 1
        if dev == HOSTS_PATH:
            hosts_zip = zname
        dev_parts = dev.split("/", 3)
        for pattern, what, source, parts in _candidates(dev_parts):
            if _matches(parts, dev.split("/")):
                hit = found.setdefault(pattern, {"path": "/" + pattern, "what": what, "source": source,
                                                 "entries": 0, "example": zname})
                hit["entries"] += 1
                # keep the shortest name: the folder or file itself
                if len(zname) < len(hit["example"]):
                    hit["example"] = zname
    if not ios:
        return None
    hosts = None
    if hosts_zip is not None:
        try:
            text = read_entry(hosts_zip).decode("utf-8", "replace")
        except Exception as exc:
            hosts = {"zip_name": hosts_zip, "error": str(exc), "extra_lines": []}
        else:
            hosts = {"zip_name": hosts_zip, "extra_lines": hosts_extra_lines(text)}
    order = {p: i for i, (p, _w, _s) in enumerate(INDICATORS)}
    return {
        "logic_version": LOGIC_VERSION,
        "system_entries": system_entries,
        "coverage": coverage,
        "signs": sorted(found.values(), key=lambda h: order[h["path"][1:]]),
        "hosts": hosts,
        "checked": len(INDICATORS) + 1,
    }


def hosts_extra_lines(text: str) -> list[str]:
    """Lines of an /etc/hosts file a stock iPhone does not have."""
    extra = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        parts = body.split()
        if len(parts) >= 2 and all((parts[0], n) in _STOCK_HOSTS for n in parts[1:]):
            continue
        extra.append(line.strip())
    return extra


def has_findings(result: dict) -> bool:
    return bool(result.get("signs")) or bool((result.get("hosts") or {}).get("extra_lines"))


def coverage_label(result: dict) -> str:
    """'full', 'partial' or 'none': how much of the system partition the
    extraction holds, judged by COVERAGE_FOLDERS."""
    present = sum(1 for n in result["coverage"].values() if n)
    if present == len(COVERAGE_FOLDERS):
        return "full"
    return "partial" if result["system_entries"] else "none"


def save(results_db, result: dict) -> None:
    """Record the result in the case's run log (one row per run)."""
    from db_utils import complete_run_log, start_run_log
    run_id = start_run_log(results_db, RUN_TYPE, total=result["checked"],
                           notes=json.dumps(result, ensure_ascii=False))
    complete_run_log(results_db, run_id, processed=result["checked"],
                     output_rows=len(result["signs"]) + len((result.get("hosts") or {}).get("extra_lines") or []))


def load(results_db) -> dict | None:
    """The latest completed result for this case at the current logic
    version, or None."""
    try:
        row = results_db.execute(
            "SELECT notes FROM run_log WHERE run_type=? AND complete=1 ORDER BY id DESC LIMIT 1",
            (RUN_TYPE,)).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        result = json.loads(row[0])
    except ValueError:
        return None
    return result if result.get("logic_version") == LOGIC_VERSION else None


NOTICE_SETTING = "ios_system_notice_shown"   # case_settings: LOGIC_VERSION once shown


def notice_html(result: dict) -> str:
    """The dialog text: what this case does not show, and what the check
    found. Plain language for the examiner; every number is from *result*."""
    from html import escape
    cov = coverage_label(result)
    n_sys = result["system_entries"]
    parts = ["<h3>What FFS Explorer does not show in this case</h3>"]

    parts.append("<p><b>1. The iOS system partition is left out.</b><br>")
    if n_sys:
        parts.append(f"{n_sys:,} entries in this extraction belong to iOS itself "
                     "(<code>/System</code>, <code>/Applications</code>, <code>/usr</code>, "
                     "<code>/etc</code>…). They are not listed in the File Browser, not processed "
                     "and not found by Keyword Search. They are still in the archive: use another "
                     "tool if you need to look at them.</p>")
    else:
        parts.append("This extraction holds no files from it, so nothing is left out here; "
                     "but nothing from it could be checked either.</p>")

    signs = result["signs"]
    extra_hosts = (result.get("hosts") or {}).get("extra_lines") or []
    parts.append("<p><b>2. Signs of jailbreaking or sideloading</b> are the main reason to look "
                 f"there, so {result['checked'] - 1} known locations were checked in both "
                 "partitions, and <code>/etc/hosts</code> was read.</p>")
    if signs or extra_hosts:
        parts.append("<p style='color:#b00020'><b>Signs found.</b> These are signs, not proof: a file "
                     "can be left from an earlier jailbreak, a security tool or the extraction "
                     "itself. Examine them with another tool and record what you find.</p><ul>")
        for h in signs:
            more = f" ({h['entries']:,} entries)" if h["entries"] > 1 else ""
            parts.append(f"<li><code>{escape(h['path'])}</code>: {escape(h['what'])}{more}<br>"
                         f"<small>in the archive as <code>{escape(h['example'])}</code></small></li>")
        if extra_hosts:
            lines = "<br>".join(f"<code>{escape(l)}</code>" for l in extra_hosts[:20])
            more = f"<br>…and {len(extra_hosts) - 20} more" if len(extra_hosts) > 20 else ""
            parts.append(f"<li><code>/etc/hosts</code> has lines a stock iPhone does not have "
                         f"(they redirect or block web addresses):<br>{lines}{more}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p><b>None found.</b> ")
        if cov == "full":
            parts.append("This extraction holds the system partition, so all the locations "
                         "could be checked")
            parts.append(", and <code>/etc/hosts</code> is the stock file." if result.get("hosts")
                         else ", but <code>/etc/hosts</code> is not in it.")
        else:
            missing = [f"<code>/{f}</code>" for f, n in result["coverage"].items() if not n]
            parts.append(f"But this extraction holds {'only part' if cov == 'partial' else 'none'} "
                         f"of the system partition (no {', '.join(missing)}), so most of the "
                         "locations could not be checked")
            parts.append("." if result.get("hosts") else ", and <code>/etc/hosts</code> is not in it.")
        parts.append(" Finding nothing does not prove the phone was never jailbroken: "
                     "a jailbreak can be removed.</p>")

    parts.append("<p><small>The result is saved in the case's run log "
                 "(<code>ios_system_check</code>). Tools ▸ iOS System Check shows this again. "
                 "Locations checked: OWASP MASTG jailbreak-detection paths, rootless "
                 "jailbreak folders, package managers and TrollStore (ASSUMPTIONS.md A-83).</small></p>")
    return "".join(parts)
