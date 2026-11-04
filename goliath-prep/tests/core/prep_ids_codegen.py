# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Generate goliath_prep/core/ids.py from the check catalogue (unit F04).

    python goliath-prep/tests/core/prep_ids_codegen.py          # rewrite ids.py
    python goliath-prep/tests/core/prep_ids_codegen.py --check  # exit 1 if it is stale

Sources, both normative and both in the repository:

* ``docs/prep/checks.json``: every check id with its family, severity, scope
  (``applies_to``), slack, witness fields and defining section (safety-model
  section 11.6);
* ``docs/prep/gi-format.md`` section 12: the ids the GI reader reports. Each of
  them must be in checks.json with the same severity.

The generated module is committed, because an installed goliath-prep has no
``docs/`` folder to read at run time. ``test_prep_ids.py`` renders the module
again from the documents and fails while the committed file differs, so the
code's ids can never drift from the catalogue.

The input is checked, not trusted: an id that is not of the catalogue's form, a
family letter that disagrees with the id, an unknown severity, a key this
generator does not know (so a new catalogue field cannot be silently dropped),
or a reader id missing from checks.json stops the generator with a message.

Requires the goliath_prep package (for the canonical-JSON catalogue hash), so it
runs in the goliath-prep test environment (Python >= 3.11).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
CHECKS_JSON = REPO / "docs" / "prep" / "checks.json"
GI_FORMAT = REPO / "docs" / "prep" / "gi-format.md"
IDS_PY = REPO / "goliath-prep" / "src" / "goliath_prep" / "core" / "ids.py"

# The same pattern as goliath-prep/tests/test_prep_doc_ids.py (families of safety-model section 1.3).
ID_RE = re.compile(r"^[WBMTDPFGSN]-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
FAMILIES = {
    "W": "wheel",
    "B": "body",
    "M": "material and structure",
    "T": "tool",
    "P": "panel geometry",
    "D": "dialect and program",
    "F": "firmware",
    "G": "feature gate",
    "S": "series",
    "N": "notices",
}
SEVERITIES = ("refuse", "warn", "disclose", "internal-error")
ENTRY_KEYS = frozenset({"id", "family", "severity", "applies_to", "condition", "slack", "witness_fields", "defined_in"})
SLACK_KEYS = frozenset({"unit", "definition"})
LINE_LIMIT = 116  # ruff checks 120; leave room


class CatalogueError(ValueError):
    """The catalogue or gi-format section 12 is not in the form this generator turns into code."""


def load_checks(path: Path = CHECKS_JSON) -> list[dict[str, Any]]:
    """checks.json, validated entry by entry."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise CatalogueError("%s: expected a non-empty JSON list of checks" % path)
    seen: set[str] = set()
    names: set[str] = set()
    for index, entry in enumerate(entries):
        where = "%s entry %d" % (path.name, index)
        if not isinstance(entry, dict):
            raise CatalogueError("%s: not an object" % where)
        keys = set(entry)
        if keys != ENTRY_KEYS:
            raise CatalogueError(
                "%s: keys %s, expected exactly %s (a new catalogue field needs this generator updated, "
                "so that it is not silently dropped from the code)" % (where, sorted(keys), sorted(ENTRY_KEYS))
            )
        check_id = entry["id"]
        if not isinstance(check_id, str) or not ID_RE.match(check_id):
            raise CatalogueError("%s: id %r is not of the form F-NAME with a family letter %s" % (
                where, check_id, "".join(FAMILIES)))
        if check_id in seen:
            raise CatalogueError("%s: id %s repeated" % (where, check_id))
        seen.add(check_id)
        name = identifier(check_id)
        if name in names:
            raise CatalogueError("%s: id %s gives the constant name %s twice" % (where, check_id, name))
        names.add(name)
        if entry["family"] != check_id[0]:
            raise CatalogueError("%s: family %r disagrees with id %s" % (where, entry["family"], check_id))
        if entry["severity"] not in SEVERITIES:
            raise CatalogueError("%s: severity %r of %s is not one of %s" % (
                where, entry["severity"], check_id, SEVERITIES))
        for key in ("applies_to", "condition", "defined_in"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise CatalogueError("%s: %s of %s must be a non-empty string" % (where, key, check_id))
        slack = entry["slack"]
        if slack is not None:
            if not isinstance(slack, dict) or set(slack) != SLACK_KEYS:
                raise CatalogueError("%s: slack of %s must be null or {unit, definition}" % (where, check_id))
            if not all(isinstance(slack[k], str) and slack[k].strip() for k in SLACK_KEYS):
                raise CatalogueError("%s: slack unit and definition of %s must be non-empty strings" % (
                    where, check_id))
        fields = entry["witness_fields"]
        if (
            not isinstance(fields, list)
            or not fields
            or not all(isinstance(f, str) and re.fullmatch(r"[a-z][a-z0-9_]*", f) for f in fields)
            or len(set(fields)) != len(fields)
        ):
            raise CatalogueError("%s: witness_fields of %s must be a non-empty list of distinct field names" % (
                where, check_id))
    return entries


def load_gi_reader_ids(checks: list[dict[str, Any]], path: Path = GI_FORMAT) -> list[str]:
    """The ids of gi-format section 12's table, in table order, checked against checks.json."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("## 12. Findings"))
    except StopIteration:
        raise CatalogueError("%s: no section '## 12. Findings'" % path) from None
    severity = {e["id"]: e["severity"] for e in checks}
    ids: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        match = re.match(r"^\|\s*`([^`]+)`\s*\|\s*([a-z-]+)\s*\|", line)
        if not match:
            continue
        reader_id, reader_severity = match.groups()
        if reader_id not in severity:
            raise CatalogueError("%s section 12 lists %s, which is not in checks.json" % (path.name, reader_id))
        if reader_severity != severity[reader_id]:
            raise CatalogueError("%s section 12 gives %s severity %s, checks.json %s" % (
                path.name, reader_id, reader_severity, severity[reader_id]))
        if reader_id in ids:
            raise CatalogueError("%s section 12 lists %s twice" % (path.name, reader_id))
        ids.append(reader_id)
    if not ids:
        raise CatalogueError("%s section 12 has no finding table" % path.name)
    return ids


def identifier(check_id: str) -> str:
    return check_id.replace("-", "_")


def catalogue_sha256(checks: list[dict[str, Any]]) -> str:
    from goliath_prep.core.canon import canonical_json

    return hashlib.sha256(canonical_json(checks)).hexdigest()


def _py(value: Any) -> str:
    """A Python literal for a str, None or tuple of str, with double quotes (json.dumps escaping)."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, tuple):
        inner = ", ".join(_py(v) for v in value)
        return "(%s,)" % inner if len(value) == 1 else "(%s)" % inner
    raise TypeError(value)


def _mapping(name: str, annotation: str, items: list[tuple[str, Any]], doc: str) -> list[str]:
    out = ["", "# %s" % doc, "%s: Final[%s] = MappingProxyType(" % (name, annotation), "    {"]
    for key, value in items:
        line = "        %s: %s," % (identifier(key), _py(value))
        if len(line) <= LINE_LIMIT or not isinstance(value, tuple):
            out.append(line)
        else:
            out.append("        %s: (" % identifier(key))
            out.extend('            %s,' % _py(v) for v in value)
            out.append("        ),")
    out += ["    }", ")"]
    return out


def render(checks: list[dict[str, Any]], reader_ids: list[str], sha256: str) -> str:
    """The text of goliath_prep/core/ids.py."""
    out = [
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "# Copyright 2026 the Goliath-CNC contributors.",
        "#",
        "# GENERATED FILE, DO NOT EDIT. Generated from docs/prep/checks.json (the check",
        "# catalogue) and docs/prep/gi-format.md section 12 (the GI reader's findings) by",
        "# goliath-prep/tests/core/prep_ids_codegen.py. After changing either document, run",
        "# from the repository root:",
        "#     python goliath-prep/tests/core/prep_ids_codegen.py",
        "# goliath-prep/tests/core/test_prep_ids.py fails while this file is stale.",
        '"""Check ids of the Goliath safety model, as code constants (unit F04).',
        "",
        "``docs/prep/checks.json`` is the normative check catalogue (safety-model",
        "section 11.6); this module is generated from it, so code names a check by a",
        "constant (``ids.W_SURF``) instead of a string that could be mistyped. Every id",
        "in the catalogue is here and nothing else is (``test_prep_doc_ids.py``).",
        "",
        "* one constant per check id, named after the id with ``-`` replaced by ``_``;",
        "* :data:`ALL`: every id, in catalogue order;",
        "* :data:`GI_READER_IDS`: the ids the GI reader reports (gi-format section 12);",
        "* :data:`SEVERITY`, :data:`APPLIES_TO`, :data:`SLACK_UNIT`,",
        "  :data:`WITNESS_FIELDS`, :data:`DEFINED_IN`: the catalogue's columns, by id",
        "  (``SLACK_UNIT`` is None for a check without a slack);",
        "* :data:`CATALOGUE_SHA256`: SHA-256 of the catalogue's canonical JSON",
        "  (:func:`goliath_prep.core.canon.canonical_json`), naming the catalogue",
        "  version this code implements.",
        '"""',
        "from __future__ import annotations",
        "",
        "from collections.abc import Mapping",
        "from types import MappingProxyType",
        "from typing import Final",
        "",
        'CATALOGUE_SHA256: Final = "%s"' % sha256,
    ]
    family = None
    for entry in checks:
        if entry["id"][0] != family:
            family = entry["id"][0]
            out += ["", "# %s: %s" % (family, FAMILIES[family])]
        out.append("%s: Final = %s" % (identifier(entry["id"]), _py(entry["id"])))
    out += ["", "# Every check id, in catalogue order.", "ALL: Final[tuple[str, ...]] = ("]
    out += ["    %s," % identifier(e["id"]) for e in checks]
    out += [")", "", "# The ids the GI reader reports (gi-format section 12), in that table's order."]
    out += ["GI_READER_IDS: Final[tuple[str, ...]] = ("]
    out += ["    %s," % identifier(i) for i in reader_ids]
    out.append(")")
    out += _mapping("SEVERITY", "Mapping[str, str]", [(e["id"], e["severity"]) for e in checks],
                    "Severity of each check: refuse, warn, disclose or internal-error (safety-model 11.1).")
    out += _mapping("APPLIES_TO", "Mapping[str, str]", [(e["id"], e["applies_to"]) for e in checks],
                    "Scope of each check (safety-model 11.1).")
    out += _mapping("SLACK_UNIT", "Mapping[str, str | None]",
                    [(e["id"], None if e["slack"] is None else e["slack"]["unit"]) for e in checks],
                    "Unit of each check's slack; None for a check without one (safety-model 11.2).")
    out += _mapping("WITNESS_FIELDS", "Mapping[str, tuple[str, ...]]",
                    [(e["id"], tuple(e["witness_fields"])) for e in checks],
                    "The witness fields each check fills (safety-model 11.3).")
    out += _mapping("DEFINED_IN", "Mapping[str, str]", [(e["id"], e["defined_in"]) for e in checks],
                    "The section of docs/prep/ that defines each check.")
    return "\n".join(out) + "\n"


def generate() -> str:
    checks = load_checks()
    return render(checks, load_gi_reader_ids(checks), catalogue_sha256(checks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if ids.py differs from the documents")
    args = parser.parse_args(argv)
    text = generate()
    current = IDS_PY.read_text(encoding="utf-8") if IDS_PY.exists() else None
    if args.check:
        if current != text:
            print("%s is stale: run python %s" % (IDS_PY, Path(__file__).relative_to(REPO).as_posix()))
            return 1
        print("%s is up to date" % IDS_PY)
        return 0
    with IDS_PY.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("wrote %s" % IDS_PY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
