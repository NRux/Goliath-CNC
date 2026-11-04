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
any letter case, as hex (how the provisioning string carries it), in its
JSON-escaped form, and as Python's ``repr()`` and ``ascii()`` write it (log lines
show messages that way), also inside a JSON string. Whole values are masked first, then the protocol fields
that carry them, so a quote or a separator inside a network name cannot cut
its masking short. In the raw wire captures a field runs to its line end or
the NUL padding; in text files a JSON quote also ends it. The decoded identity
and scan records in ``events.jsonl`` have their private fields masked whatever
their length. A value shorter than 4 characters would destroy unrelated data if
masked everywhere; it is masked where the protocol puts a serial or a network
name (and the manifest counts how many such values there were). Lines of
``events.jsonl`` that are not event records (a torn last line) are covered by
the byte patterns only; the manifest counts them and the log says so.

**Verified.** The archive is built under a temporary name and read back: if
any value or protocol field that should be masked is still readable, the
archive is deleted and :class:`ExportRedactionError` names the file (never the
value). ``private.json`` and the export log are never exported. Neither is a
compressed file (zip, gzip, ...): its content cannot be checked, so it is left
out, listed in the manifest and logged. The archive cannot be written inside the
session record (a later export would sweep it in unchecked). The archive
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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from goliath_run import __version__
from goliath_run.record import EVENTS_NAME, EXPORTS_NAME, PRIVATE_NAME, RECORD_NAME, WIRE_DIR, mark_exported

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
# what gets masked. Keyed by context: True for raw link bytes, where a field ends at a line end or the NUL
# padding of a 512-byte send; False for text files (JSON lines, logs), where a JSON quote or backslash ends it
# too. The serial is the last field of INFO;34, so it runs to the end of its line.
_INFO34 = {
    True: re.compile(rb"INFO;34;[^;\r\n\x00]*;[^;\r\n\x00]*;[^;\r\n\x00]*;([^\r\n\x00]+)"),
    False: re.compile(rb"INFO;34;[^;\r\n\x00\"]*;[^;\r\n\x00\"]*;[^;\r\n\x00\"]*;([^\r\n\x00\"\\]+)"),
}
_INFO31 = {
    True: re.compile(rb"INFO;31;([^\r\n\x00]+)"),
    False: re.compile(rb"INFO;31;([^\r\n\x00\"\\]+)"),
}
_PASSWORD = {
    True: re.compile(rb"PWD=([^&\s\x00]+)"),
    False: re.compile(rb"PWD=([^&\s\x00\"\\]+)"),
}
_ROBOT_SSID = re.compile(rb"(?i)goliath_[0-9a-z]{6}(?![0-9a-z_])")
_PROVISION = re.compile(rb"SSID=([0-9A-Fa-f]*)&PWD=([0-9A-Fa-f]*)")
_SCAN_MARKERS = (b"START", b"END")
# Compressed formats hide their content from the redaction and from the read-back (zip, gzip, bzip2, xz, 7z, zstd).
_COMPRESSED_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00",
                     b"7z\xbc\xaf\x27\x1c", b"\x28\xb5\x2f\xfd")
_COMPRESSED_SUFFIXES = frozenset({".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".zst"})
_INFO_IDENTITY = 34
_INFO_SCAN = 31


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
    """The forms a value is masked in: as written (any case, by the regex flag), hex, JSON-escaped, and as Python
    writes it with ``repr()`` or ``ascii()`` (log lines show messages that way), also inside a JSON string."""
    forms = [value, value.hex().encode("ascii")]
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return forms  # not text: it cannot appear JSON-escaped or in a repr either
    escaped: list[str] = []
    for shown in (text, repr(text)[1:-1], ascii(text)[1:-1]):
        escaped += [shown, json.dumps(shown)[1:-1], json.dumps(shown, ensure_ascii=False)[1:-1]]
    for form in escaped:
        encoded = form.encode("utf-8")
        if encoded not in forms:
            forms.append(encoded)
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

    Each value is matched as written in any letter case, as hex, JSON-escaped and in Python's repr form (also
    JSON-escaped). Values shorter than
    :data:`MIN_VALUE_LENGTH` are left to :func:`redact_patterns`.
    """
    buf = bytearray(data)
    count = 0
    for rx in _value_regexes(values):
        for m in list(rx.finditer(bytes(buf))):
            _mask(buf, m.start(), m.end())
            count += 1
    return bytes(buf), count


def redact_patterns(data: bytes, *, serial: bool, ssids: bool, wire: bool) -> tuple[bytes, int]:
    """Mask the protocol fields that carry private values, whatever their length; and the count.

    ``serial``: the serial field of ``INFO;34``. ``ssids``: ``INFO;31`` scan entries (not ``START``/``END``),
    ``Goliath_xxxxxx`` names and the SSID of a provisioning string. A provisioning password is always masked.
    ``wire``: True for raw link bytes (a field ends at a line end or NUL), False for text files (a JSON quote or
    backslash ends it too). Run :func:`redact_bytes` with the whole values first: a value that holds a quote is
    otherwise only partly masked in a text file.
    """
    if type(wire) is not bool:
        raise TypeError("wire must be a bool (True: raw link bytes; False: a text file)")
    buf = bytearray(data)
    count = 0

    def mask_group(m: re.Match[bytes], group: int) -> None:
        nonlocal count
        start, end = m.span(group)
        if end > start and not _is_masked(bytes(buf[start:end])):
            _mask(buf, start, end)
            count += 1

    if serial:
        for m in _INFO34[wire].finditer(data):
            mask_group(m, 1)
    if ssids:
        for m in _INFO31[wire].finditer(data):
            if m.group(1) not in _SCAN_MARKERS:
                mask_group(m, 1)
        for m in _ROBOT_SSID.finditer(data):
            mask_group(m, 0)
    for m in _PROVISION.finditer(data):
        if ssids:
            mask_group(m, 1)
        mask_group(m, 2)
    for m in _PASSWORD[wire].finditer(bytes(buf)):
        mask_group(m, 1)
    return bytes(buf), count


def harvest_private_values(data: bytes, *, wire: bool) -> dict[str, set[bytes]]:
    """The serials, SSIDs and passwords the protocol fields of ``data`` carry (raw bytes).

    ``wire`` as for :func:`redact_patterns`.
    """
    if type(wire) is not bool:
        raise TypeError("wire must be a bool (True: raw link bytes; False: a text file)")
    found: dict[str, set[bytes]] = {"serial": set(), "ssid": set(), "password": set()}
    for m in _INFO34[wire].finditer(data):
        found["serial"].add(m.group(1).strip())
    for m in _INFO31[wire].finditer(data):
        if m.group(1) not in _SCAN_MARKERS:
            found["ssid"].add(m.group(1))
    for m in _ROBOT_SSID.finditer(data):
        found["ssid"].add(m.group(0))
    for m in _PROVISION.finditer(data):
        for kind, group in (("ssid", 1), ("password", 2)):
            text = m.group(group)
            if text and len(text) % 2 == 0:
                found[kind].add(bytes.fromhex(text.decode("ascii")))
            # An odd-length hex field cannot be decoded; redact_patterns masks it in place all the same.
    for m in _PASSWORD[wire].finditer(data):
        found["password"].add(m.group(1))
    for kind in found:
        found[kind].discard(b"")
    return found


# ---------------------------------------------------------------------------
# Decoded events (events.jsonl)
# ---------------------------------------------------------------------------


def _event_lines(data: bytes) -> Iterator[tuple[bytes, bytes, dict[str, Any] | None]]:
    """Each line of events.jsonl: its body, its line ending, and the record when the body is a JSON object.

    A body that is not a JSON object (a torn last line, a foreign line) yields None; callers count those lines
    (they are redacted by the byte patterns only) and the export reports the count.
    """
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        entry: dict[str, Any] | None = None
        if body.strip():
            try:
                parsed = json.loads(body)
            except ValueError:  # JSONDecodeError or UnicodeDecodeError: counted by the caller, never ignored
                parsed = None
            if isinstance(parsed, dict):
                entry = parsed
        yield body, line[len(body):], entry


def _private_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The fields of a decoded INFO;34 or INFO;31 record (as JobRecord.event writes them), else None.

    Raises ValueError for such a record whose fields are not what the recorder writes.
    """
    fields = entry.get("fields")
    if entry.get("type") != "TextMessage" or not isinstance(fields, dict) or fields.get("kind") != "INFO":
        return None
    if fields.get("code") not in (_INFO_IDENTITY, _INFO_SCAN):
        return None
    params, raw = fields.get("params"), fields.get("raw")
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params) or not isinstance(raw, str):
        raise ValueError("an INFO;%s record without the recorder's params and raw fields" % fields.get("code"))
    return fields


def _private_part(fields: dict[str, Any], *, serial: bool, ssids: bool) -> tuple[int, int] | None:
    """How many leading params and ';'-separated raw parts of the record are kept; None when nothing is private."""
    params = fields["params"]
    if fields["code"] == _INFO_IDENTITY:
        # INFO;34;<prj>;<HW>;<SW>;<SN>: the serial is the fourth param and everything after it.
        return (3, 5) if serial and len(params) > 3 else None
    if ssids and ";".join(params).encode("utf-8") not in _SCAN_MARKERS:
        return (0, 2)  # INFO;31;<ssid>: the whole tail is the network name
    return None


def _mask_events(data: bytes, *, serial: bool, ssids: bool) -> tuple[bytes, int, int]:
    """events.jsonl with the private fields of decoded identity and scan records masked, whatever their length.

    Returns the new bytes, the number of records masked and the number of lines that are not event records.
    """
    out: list[bytes] = []
    masked = unparsed = 0
    for body, ending, entry in _event_lines(data):
        if entry is None:
            unparsed += 1 if body.strip() else 0
            out.append(body + ending)
            continue
        try:
            fields = _private_record(entry)
        except ValueError:
            unparsed += 1
            out.append(body + ending)
            continue
        part = None if fields is None else _private_part(fields, serial=serial, ssids=ssids)
        if fields is None or part is None:
            out.append(body + ending)
            continue
        keep_params, keep_raw = part
        params, raw = fields["params"], fields["raw"]
        fields["params"] = params[:keep_params] + ["*" * len(p) for p in params[keep_params:]]
        pieces = raw.split(";", keep_raw)
        if len(pieces) == keep_raw + 1:
            fields["raw"] = ";".join(pieces[:keep_raw]) + ";" + "*" * len(pieces[keep_raw])
        # (A raw text without that many fields holds no private part; the byte patterns check it all the same.)
        if fields["params"] == params and fields["raw"] == raw:
            out.append(body + ending)
            continue
        masked += 1
        out.append(json.dumps(entry, ensure_ascii=False).encode("utf-8") + ending)
    return b"".join(out), masked, unparsed


def _unmasked_event_records(data: bytes, *, serial: bool, ssids: bool) -> int:
    """How many decoded identity or scan records in ``data`` still show a private field (for the read-back)."""
    left = 0
    for _body, _ending, entry in _event_lines(data):
        try:
            fields = None if entry is None else _private_record(entry)
        except ValueError:
            continue  # the byte patterns check such a line
        part = None if fields is None else _private_part(fields, serial=serial, ssids=ssids)
        if fields is None or part is None:
            continue
        pieces = fields["raw"].split(";", part[1])
        raw_left = len(pieces) == part[1] + 1 and not _is_masked(pieces[part[1]].encode("utf-8"))
        if raw_left or any(not _is_masked(p.encode("utf-8")) for p in fields["params"][part[0]:]):
            left += 1
    return left


def _harvest_events(data: bytes) -> dict[str, set[bytes]]:
    """Serials and SSIDs in the structured fields of decoded TextMessage events (events.jsonl)."""
    found: dict[str, set[bytes]] = {"serial": set(), "ssid": set()}
    for _body, _ending, entry in _event_lines(data):
        try:
            fields = None if entry is None else _private_record(entry)
        except ValueError:
            continue  # counted by _mask_events, reported in the manifest
        if fields is None:
            continue
        params = fields["params"]
        if fields["code"] == _INFO_IDENTITY and len(params) > 3 and ";".join(params[3:]).strip():
            found["serial"].add(";".join(params[3:]).strip().encode("utf-8"))
        elif fields["code"] == _INFO_SCAN and params and ";".join(params).encode("utf-8") not in _SCAN_MARKERS:
            found["ssid"].add(";".join(params).encode("utf-8"))
    return found


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------


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


def _mask_name(text: str, to_mask: set[bytes], *, serial: bool, ssids: bool) -> str:
    """A file name or path as the archive may show it: known values and protocol fields masked."""
    data, _ = redact_bytes(text.encode("utf-8"), to_mask)
    data, _ = redact_patterns(data, serial=serial, ssids=ssids, wire=False)
    return data.decode("utf-8")


def _is_wire_capture(rel: Path) -> bool:
    return len(rel.parts) == 2 and rel.parts[0] == WIRE_DIR and rel.suffix == ".bin"


def _is_compressed(rel: Path, data: bytes) -> bool:
    return rel.suffix.lower() in _COMPRESSED_SUFFIXES or data.startswith(_COMPRESSED_MAGIC)


def _target(job_dir: Path, out: Path) -> Path:
    record = job_dir.resolve()
    where = out.resolve()
    if where == record or record in where.parents:
        # A later export would sweep this archive, compressed and unreadable to the read-back, into itself.
        raise ValueError("%s is inside the session record %s: export to a folder outside it" % (out, job_dir))
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
            data = path.read_bytes()
            if not _is_wire_capture(rel) and _is_compressed(rel, data):
                excluded.append("%s (a compressed file: its content cannot be checked for serials and network "
                                "names)" % rel.as_posix())
                log.warning("%s of %s is compressed: left out of the export, since its content cannot be checked "
                            "for serials and network names", rel.as_posix(), job_dir)
                continue
            files.append((rel, data))

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
        for kind, found in harvest_private_values(data, wire=_is_wire_capture(rel)).items():
            values[kind].update(found)
        if rel.as_posix() == EVENTS_NAME:
            for kind, found in _harvest_events(data).items():
                values[kind].update(found)
    to_mask = set(values["password"])
    if redact_serial:
        to_mask |= values["serial"]
    if redact_ssids:
        to_mask |= values["ssid"]
    home = _home_forms() if redact_home else []

    replacements = 0
    events_unparsed = 0
    members: list[tuple[str, bytes, str]] = []  # (name, data, kind: 'wire', 'events' or 'text')
    for rel, data in files:
        wire = _is_wire_capture(rel)
        kind = "wire" if wire else "events" if rel.as_posix() == EVENTS_NAME else "text"
        n0 = 0
        if kind == "events":
            data, n0, events_unparsed = _mask_events(data, serial=redact_serial, ssids=redact_ssids)
        # Whole values first: a protocol pattern stops at a quote or separator and would otherwise leave the
        # rest of a value that holds one readable.
        data, n1 = redact_bytes(data, to_mask)
        data, n2 = redact_patterns(data, serial=redact_serial, ssids=redact_ssids, wire=wire)
        n3 = 0
        if home and not wire:
            data, n3 = _mask_home(data, home)
        arcname = "%s/%s" % (job_dir.name, rel.as_posix())
        masked_name, n4 = redact_bytes(arcname.encode("utf-8"), to_mask)
        masked_name, n5 = redact_patterns(masked_name, serial=redact_serial, ssids=redact_ssids, wire=False)
        replacements += n0 + n1 + n2 + n3 + n4 + n5
        members.append((masked_name.decode("utf-8"), data, kind))
    if events_unparsed:
        log.warning("%s of %s: %d line(s) are not event records (a torn last line or a foreign line); they were "
                    "redacted by the protocol patterns and the known values only", EVENTS_NAME, job_dir,
                    events_unparsed)
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
        "event_lines_not_parsed": events_unparsed,
        "replacements": replacements,
        "excluded": [_mask_name(e, to_mask, serial=redact_serial, ssids=redact_ssids) for e in excluded],
        # Worded so that it matches none of the protocol patterns the archive is verified against.
        "method": "each value is replaced by '*' of the same length wherever it occurs as written (any letter "
                  "case), as hex, JSON-escaped or as a program log writes it, and in the protocol fields that carry "
                  "serials and network names "
                  "(the identity message INFO 34, the network scan INFO 31, robot network names, and the network "
                  "name and password of a provisioning string), including the decoded identity and scan records "
                  "of the event log; the home folder becomes <HOME> outside the wire captures",
    }
    members.append((MANIFEST_NAME, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"), "text"))

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (".%s.%d.%s.tmp" % (target.name, os.getpid(), secrets.token_hex(4)))
    try:
        with zipfile.ZipFile(tmp, "x", compression=zipfile.ZIP_DEFLATED) as z:
            for name, data, _kind in members:
                z.writestr(name, data)
        _verify(tmp, {name: kind for name, _data, kind in members}, to_mask, home, serial=redact_serial,
                ssids=redact_ssids)
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


def _verify(zip_path: Path, kinds: dict[str, str], to_mask: set[bytes], home: list[bytes], *, serial: bool,
            ssids: bool) -> None:
    """Read the archive back; raise ExportRedactionError naming the member if anything readable remains."""
    regexes = _value_regexes(to_mask)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if sorted(names) != sorted(kinds):
            raise ExportRedactionError("the archive's members are not the ones written; nothing was exported")
        for name in names:
            data = z.read(name)
            kind = kinds[name]
            problems: list[str] = []
            for target, wire in ((data, kind == "wire"), (name.encode("utf-8"), False)):
                if any(rx.search(target) for rx in regexes):
                    problems.append("a redacted value")
                _, left = redact_patterns(target, serial=serial, ssids=ssids, wire=wire)
                if left:
                    problems.append("%d unmasked protocol field(s)" % left)
            if kind == "events":
                left = _unmasked_event_records(data, serial=serial, ssids=ssids)
                if left:
                    problems.append("%d decoded record(s) with an unmasked field" % left)
            if home and kind != "wire" and any(form in data for form in home):
                problems.append("the home folder")
            if problems:
                raise ExportRedactionError(
                    "the export would still contain %s in %s; nothing was exported (please report this)"
                    % (" and ".join(sorted(set(problems))), name))
