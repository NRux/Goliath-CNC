# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The diagnostic export: a session record as a zip, safe to attach to a public bug report (unit R02).

The project design, section 7.9: ``goliath-run diag export <job>`` zips a
session with the robot serial and the Wi-Fi network names (SSIDs) redacted by
default. A provisioning password (``SSID=<hex>&PWD=<hex>``) is always masked,
whatever the options. The user's home folder is replaced by ``<HOME>`` in
every file except the raw wire captures.

**Which values.** The record's ``private.json`` (serials and SSIDs the runner
noted), values passed by the caller, and every value found in the session's
files: the serial field of ``INFO;34`` lines, ``INFO;31`` scan entries,
``Goliath_xxxxxx`` network names, the SSID and password of a provisioning
string, and the same fields of decoded events in ``events.jsonl``.

**How.** Each value is replaced by ``*`` characters of the same length (so
offsets in ``wire/index.jsonl`` stay valid) wherever it occurs: as written, in
any letter case, as hex (how the provisioning string carries it) and in its
JSON-escaped form. A value shorter than 4 characters would destroy unrelated
data if masked everywhere; it is masked where the protocol puts a serial or a
network name (and the manifest counts how many such values there were).

**Verified.** The archive is built under a temporary name and read back: if
any value or protocol field that should be masked is still readable, the
archive is deleted and :class:`ExportRedactionError` names the file (never the
value). ``private.json`` and the export log are never exported. The archive
carries ``REDACTION.json`` saying what was redacted and how, without the values.

:func:`redact_bytes` and :func:`redact_patterns` are the redaction itself, pure
and dependency-free, so the prep bug bundle can apply the same rules.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import zipfile
from collections.abc import Iterable
from pathlib import Path

from goliath_run import __version__
from goliath_run.record import EXPORTS_NAME, PRIVATE_NAME, RECORD_NAME, WIRE_DIR, mark_exported

__all__ = [
    "MANIFEST_NAME",
    "MIN_VALUE_LENGTH",
    "ExportRedactionError",
    "export_diag",
    "harvest_private_values",
    "redact_bytes",
    "redact_patterns",
]

log = logging.getLogger("goliath_run.export")

MANIFEST_NAME = "REDACTION.json"
MIN_VALUE_LENGTH = 4
"""Values shorter than this are masked only in the protocol fields that carry them, not everywhere."""
_MASK = 0x2A  # '*'
_HOME_MASK = b"<HOME>"

# Protocol fields that carry private values (spec 02, INFO table; spec 04, provisioning). Group 1 (and 2) are
# what gets masked. The character classes stop at line ends, NUL padding, field separators and JSON quotes.
_INFO34 = re.compile(rb"INFO;34;[^;\r\n\x00\"]*;[^;\r\n\x00\"]*;[^;\r\n\x00\"]*;([^;\r\n\x00\"\\]+)")
_INFO31 = re.compile(rb"INFO;31;([^\r\n\x00\"\\]+)")
_ROBOT_SSID = re.compile(rb"(?i)goliath_[0-9a-z]{6}(?![0-9a-z_])")
_PROVISION = re.compile(rb"SSID=([0-9A-Fa-f]*)&PWD=([0-9A-Fa-f]*)")
_PASSWORD = re.compile(rb"PWD=([^&\s\x00\"\\]+)")
_SCAN_MARKERS = (b"START", b"END")


class ExportRedactionError(RuntimeError):
    """The archive would still contain a value that should be redacted; nothing was exported."""


def _mask(buf: bytearray, start: int, end: int) -> None:
    buf[start:end] = bytes([_MASK]) * (end - start)


def _is_masked(data: bytes) -> bool:
    return all(b == _MASK for b in data)


def _as_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("a value to redact must be str or bytes, got %s" % type(value).__name__)


def _forms(value: bytes) -> list[bytes]:
    """The forms a value is masked in: as written (any case, by the regex flag), hex, and JSON-escaped."""
    forms = [value, value.hex().encode("ascii")]
    try:
        escaped = json.dumps(value.decode("utf-8"))[1:-1].encode("ascii")
    except UnicodeDecodeError:
        escaped = value
    if escaped != value:
        forms.append(escaped)
    return forms


def _value_regexes(values: Iterable[str | bytes]) -> list[re.Pattern[bytes]]:
    seen: set[bytes] = set()
    regexes: list[re.Pattern[bytes]] = []
    # Longest first, so a value inside a longer one does not leave the longer one's rest readable.
    for value in sorted({_as_bytes(v) for v in values}, key=lambda v: (-len(v), v)):
        if len(value) < MIN_VALUE_LENGTH:
            continue
        for form in _forms(value):
            if form not in seen:
                seen.add(form)
                regexes.append(re.compile(re.escape(form), re.IGNORECASE))
    return regexes


def redact_bytes(data: bytes, values: Iterable[str | bytes]) -> tuple[bytes, int]:
    """``data`` with every occurrence of every value masked by ``*`` of the same length; and the count.

    Each value is matched as written in any letter case, as hex and JSON-escaped. Values shorter than
    :data:`MIN_VALUE_LENGTH` are left to :func:`redact_patterns`.
    """
    buf = bytearray(data)
    count = 0
    for rx in _value_regexes(values):
        for m in list(rx.finditer(bytes(buf))):
            _mask(buf, m.start(), m.end())
            count += 1
    return bytes(buf), count


def redact_patterns(data: bytes, *, serial: bool, ssids: bool) -> tuple[bytes, int]:
    """Mask the protocol fields that carry private values, whatever their length; and the count.

    ``serial``: the serial field of ``INFO;34``. ``ssids``: ``INFO;31`` scan entries (not ``START``/``END``),
    ``Goliath_xxxxxx`` names and the SSID of a provisioning string. A provisioning password is always masked.
    """
    buf = bytearray(data)
    count = 0

    def mask_group(m: re.Match[bytes], group: int) -> None:
        nonlocal count
        start, end = m.span(group)
        if end > start and not _is_masked(bytes(buf[start:end])):
            _mask(buf, start, end)
            count += 1

    if serial:
        for m in _INFO34.finditer(data):
            mask_group(m, 1)
    if ssids:
        for m in _INFO31.finditer(data):
            if m.group(1) not in _SCAN_MARKERS:
                mask_group(m, 1)
        for m in _ROBOT_SSID.finditer(data):
            mask_group(m, 0)
    for m in _PROVISION.finditer(data):
        if ssids:
            mask_group(m, 1)
        mask_group(m, 2)
    for m in _PASSWORD.finditer(bytes(buf)):
        mask_group(m, 1)
    return bytes(buf), count


def harvest_private_values(data: bytes) -> dict[str, set[bytes]]:
    """The serials, SSIDs and passwords the protocol fields of ``data`` carry (raw bytes)."""
    found: dict[str, set[bytes]] = {"serial": set(), "ssid": set(), "password": set()}
    for m in _INFO34.finditer(data):
        found["serial"].add(m.group(1).strip())
    for m in _INFO31.finditer(data):
        if m.group(1) not in _SCAN_MARKERS:
            found["ssid"].add(m.group(1))
    for m in _ROBOT_SSID.finditer(data):
        found["ssid"].add(m.group(0))
    for m in _PROVISION.finditer(data):
        for kind, group in (("ssid", 1), ("password", 2)):
            text = m.group(group)
            if text and len(text) % 2 == 0:
                found[kind].add(bytes.fromhex(text.decode("ascii")))
    for m in _PASSWORD.finditer(data):
        found["password"].add(m.group(1))
    for kind in found:
        found[kind].discard(b"")
    return found


def _harvest_events(text: bytes) -> dict[str, set[bytes]]:
    """Serials and SSIDs in the structured fields of decoded TextMessage events (events.jsonl)."""
    found: dict[str, set[bytes]] = {"serial": set(), "ssid": set()}
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # a torn last line: its raw text is still covered by the byte patterns
        if not isinstance(entry, dict):
            continue  # not a record line: its raw text is still covered by the byte patterns
        fields = entry.get("fields")
        if entry.get("type") != "TextMessage" or not isinstance(fields, dict) or fields.get("kind") != "INFO":
            continue
        params = fields.get("params")
        if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
            continue
        if fields.get("code") == 34 and len(params) >= 4 and params[3].strip():
            found["serial"].add(params[3].strip().encode("utf-8"))
        elif fields.get("code") == 31 and params and ";".join(params) not in ("START", "END"):
            found["ssid"].add(";".join(params).encode("utf-8"))
    return found


def _home_forms() -> list[bytes]:
    home = str(Path.home())
    if len(home.strip("/\\")) < 2:
        return []
    forms = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    return sorted((f.encode("utf-8") for f in forms), key=lambda f: -len(f))


def _mask_home(data: bytes, forms: list[bytes]) -> tuple[bytes, int]:
    count = 0
    for form in forms:
        count += data.count(form)
        data = data.replace(form, _HOME_MASK)
    return data, count


def _is_wire_capture(rel: Path) -> bool:
    return rel.parts[0] == WIRE_DIR and rel.suffix == ".bin"


def _target(job_dir: Path, out: Path) -> Path:
    if out.is_dir():
        return out / ("%s-diag.zip" % job_dir.name)
    if out.suffix.lower() == ".zip":
        return out
    if out.suffix == "":
        return out / ("%s-diag.zip" % job_dir.name)
    raise ValueError("%s: give a folder or a .zip file name for the export" % out)


def export_diag(job_dir: Path, out: Path, *, redact_serial: bool = True, redact_ssids: bool = True,
                extra_serials: Iterable[str] = (), extra_ssids: Iterable[str] = (), redact_home: bool = True) -> Path:
    """Zip the session record ``job_dir`` into ``out`` (a folder, or a ``.zip`` path); return the archive's path.

    Serials and SSIDs are redacted unless switched off; passwords always. ``extra_serials`` and ``extra_ssids``
    add values the caller knows (the ledger's serial, the laptop's network). An existing archive is never
    replaced (FileExistsError). The session is then marked exported, for the retention policy.
    """
    job_dir, out = Path(job_dir), Path(out)
    for name, flag in (("redact_serial", redact_serial), ("redact_ssids", redact_ssids),
                       ("redact_home", redact_home)):
        if type(flag) is not bool:
            raise TypeError("%s must be a bool" % name)
    if not (job_dir / RECORD_NAME).is_file():
        raise ValueError("%s is not a session record (no %s)" % (job_dir, RECORD_NAME))
    target = _target(job_dir, out)
    if target.exists():
        raise FileExistsError("%s already exists; nothing was exported" % target)

    excluded: list[str] = []
    files: list[tuple[Path, bytes]] = []
    for path in sorted(p for p in job_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(job_dir)
        if rel.as_posix() == PRIVATE_NAME:
            excluded.append("%s (the values to redact)" % PRIVATE_NAME)
        elif rel.as_posix() == EXPORTS_NAME:
            excluded.append("%s (the export log)" % EXPORTS_NAME)
        elif path.name.startswith(".") and path.name.endswith(".tmp"):
            excluded.append("%s (a temporary file of an interrupted write)" % rel.as_posix())
        else:
            files.append((rel, path.read_bytes()))

    # Every value to redact.
    values: dict[str, set[bytes]] = {"serial": set(), "ssid": set(), "password": set()}
    private_path = job_dir / PRIVATE_NAME
    if private_path.exists():
        try:
            private = json.loads(private_path.read_text(encoding="utf-8"))
            values["serial"].update(_as_bytes(v) for v in private["serials"])
            values["ssid"].update(_as_bytes(v) for v in private["ssids"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ExportRedactionError("%s cannot be read (%s): the values to redact are unknown; nothing was "
                                       "exported" % (private_path, exc)) from None
    values["serial"].update(_as_bytes(v) for v in extra_serials)
    values["ssid"].update(_as_bytes(v) for v in extra_ssids)
    for rel, data in files:
        for kind, found in harvest_private_values(data).items():
            values[kind].update(found)
        if rel.as_posix() == "events.jsonl":
            for kind, found in _harvest_events(data).items():
                values[kind].update(found)
    to_mask = set(values["password"])
    if redact_serial:
        to_mask |= values["serial"]
    if redact_ssids:
        to_mask |= values["ssid"]
    home = _home_forms() if redact_home else []

    replacements = 0
    members: list[tuple[str, bytes]] = []
    for rel, data in files:
        data, n1 = redact_patterns(data, serial=redact_serial, ssids=redact_ssids)
        data, n2 = redact_bytes(data, to_mask)
        n3 = 0
        if home and not _is_wire_capture(rel):
            data, n3 = _mask_home(data, home)
        arcname = "%s/%s" % (job_dir.name, rel.as_posix())
        masked_name, n4 = redact_bytes(arcname.encode("utf-8"), to_mask)
        replacements += n1 + n2 + n3 + n4
        members.append((masked_name.decode("utf-8"), data))
    short = sorted(v for v in to_mask if len(v) < MIN_VALUE_LENGTH)
    manifest = {
        "format": 1,
        "software": {"goliath_run": __version__},
        "serial_redacted": redact_serial,
        "ssids_redacted": redact_ssids,
        "passwords_redacted": True,
        "home_redacted": bool(home),
        "values": {kind: len(found) for kind, found in sorted(values.items())},
        "short_values_masked_in_protocol_fields_only": len(short),
        "replacements": replacements,
        "excluded": excluded,
        # Worded so that it matches none of the protocol patterns the archive is verified against.
        "method": "each value is replaced by '*' of the same length wherever it occurs as written (any letter "
                  "case), as hex or JSON-escaped, and in the protocol fields that carry serials and network names "
                  "(the identity message INFO 34, the network scan INFO 31, robot network names, and the network "
                  "name and password of a provisioning string); the home folder becomes <HOME> outside the wire "
                  "captures",
    }
    members.append((MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")))

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (".%s.%d.%s.tmp" % (target.name, os.getpid(), secrets.token_hex(4)))
    try:
        with zipfile.ZipFile(tmp, "x", compression=zipfile.ZIP_DEFLATED) as z:
            for name, data in members:
                z.writestr(name, data)
        _verify(tmp, to_mask, home, serial=redact_serial, ssids=redact_ssids)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass  # never created, or already renamed
        except OSError as cleanup:
            log.warning("could not remove the unfinished export %s: %s", tmp, cleanup)
        raise
    mark_exported(job_dir, target.name, digest)
    log.info("exported %s to %s (serial %s, SSIDs %s, %d replacements)", job_dir, target,
             "redacted" if redact_serial else "NOT redacted", "redacted" if redact_ssids else "NOT redacted",
             replacements)
    return target


def _verify(zip_path: Path, to_mask: set[bytes], home: list[bytes], *, serial: bool, ssids: bool) -> None:
    """Read the archive back; raise ExportRedactionError naming the member if anything readable remains."""
    regexes = _value_regexes(to_mask)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            data = z.read(name)
            problems: list[str] = []
            for target in (data, name.encode("utf-8")):
                if any(rx.search(target) for rx in regexes):
                    problems.append("a redacted value")
                _, left = redact_patterns(target, serial=serial, ssids=ssids)
                if left:
                    problems.append("%d unmasked protocol field(s)" % left)
            if home and not (name.split("/")[1:2] == [WIRE_DIR] and name.endswith(".bin")):
                if any(form in data for form in home):
                    problems.append("the home folder")
            if problems:
                raise ExportRedactionError(
                    "the export would still contain %s in %s; nothing was exported (please report this)"
                    % (" and ".join(sorted(set(problems))), name))
