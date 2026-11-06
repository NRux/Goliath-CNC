#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inventory owner-supplied, already extracted Slingshot installers with REA.

Reads files; never loads assemblies, starts Slingshot, or contacts a robot.
Full REA evidence belongs in a private/ignored directory. The manifest contains
file identities and selected metadata, without decompiled code or string pools.
Requires a built REA checkout and its supported Node runtime. No dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ASSEMBLIES = (
    "Slingshot.exe", "GoliathCommLib.dll", "GoliathCommLibTCP.dll",
    "PPGoliath.dll", "WorkLogicLibrary.dll", "System.Geometry.dll",
    "SysLavISOBase.dll", "SysLavGest.dll", "GoliathFirmwareTool.exe",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata_summary(evidence: dict) -> dict:
    """Explicit allowlist: do not publish attributes, resources or literal text."""
    result = evidence["normalized_result"]
    return {
        "evidence_id": evidence["evidence_id"],
        "classification": result.get("classification"),
        "assembly": {key: result.get("assembly", {}).get(key)
                     for key in ("name", "version")}
                    if result.get("assembly") else None,
        "mvid": (result.get("module") or {}).get("mvid"),
        "target_frameworks": result.get("target_frameworks"),
        "metadata_status": (result.get("metadata") or {}).get("status"),
        "table_row_counts": (result.get("metadata") or {}).get("table_row_counts"),
        "references": [{"name": row["name"], "version": row["version"]}
                       for row in result.get("references", [])],
        "coverage": result.get("coverage"),
        "limitations": evidence.get("limitations", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted-root", type=Path, required=True)
    parser.add_argument("--installer-root", type=Path, required=True)
    parser.add_argument("--rea-root", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if not args.extracted_root.is_dir() or not args.installer_root.is_dir():
        parser.error("Extracted root and installer root must be existing directories")
    roots = sorted(args.extracted_root.resolve().glob("*/app"))
    if not roots:
        parser.error("No extracted <version>/app directories found")
    for root in roots:
        if not root.is_dir() or not any((root / name).is_file() for name in ASSEMBLIES):
            parser.error(f"No selected assembly found in extracted directory: {root}")
    installers = sorted(path for path in args.installer_root.rglob("*.exe")
                        if path.is_file())
    if not installers:
        parser.error("Installer root contains no .exe files; provenance is required")
    if args.manifest.exists() or args.evidence_dir.exists():
        parser.error("Manifest and evidence directory must be new paths")
    rea_root = args.rea_root.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(rea_root), "rev-parse", "HEAD"], text=True,
    ).strip()
    package = json.loads((rea_root / "package.json").read_text(encoding="utf-8"))
    args.evidence_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "scope": "Static installer inventory; no target execution or hardware validation",
        "rea": {"repository": "https://github.com/morluto/rea",
                "commit": revision, "package_version": package["version"]},
        "installers": [], "versions": [],
    }
    for path in installers:
        manifest["installers"].append({
            "path": path.relative_to(args.installer_root).as_posix(),
            "size": path.stat().st_size, "sha256": sha256(path),
        })
    for root in roots:
        version = root.parent.name
        files = [{"path": path.relative_to(root).as_posix(),
                  "size": path.stat().st_size, "sha256": sha256(path)}
                 for path in sorted(root.rglob("*")) if path.is_file()]
        assemblies = []
        for name in ASSEMBLIES:
            target = root / name
            if not target.is_file():
                continue
            evidence_file = args.evidence_dir / f"{version}-{name}-artifact.json"
            command = [str(args.node.resolve()), str(rea_root / "scripts/rea.mjs"),
                       "inspect-managed-artifact", str(target), "--json"]
            with evidence_file.open("w", encoding="utf-8") as output:
                subprocess.run(command, stdout=output, check=True, text=True,
                               encoding="utf-8", timeout=120)
            evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
            # A successful command alone is not proof that the target was read.
            actual = evidence["normalized_result"]["artifact"]["sha256"]
            if actual != sha256(target):
                raise ValueError(f"REA artifact identity mismatch: {version}/{name}")
            assemblies.append({"file": name, "sha256": actual,
                               **metadata_summary(evidence)})
            print(f"Inspected {version}/{name}", flush=True)
        manifest["versions"].append({"version": version, "files": files,
                                      "assemblies": assemblies})
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.manifest}")


if __name__ == "__main__":
    main()
