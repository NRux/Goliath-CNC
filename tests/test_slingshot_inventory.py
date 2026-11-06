# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad research inputs must fail before running REA or creating evidence.

Fixtures are empty directories and authored placeholder bytes, not vendor files.
"""

from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "slingshot_inventory.py"


@pytest.mark.parametrize(
    "case, message",
    [
        ("missing_archive", "must be existing directories"),
        ("empty_archive", "provenance is required"),
        ("empty_app", "No selected assembly"),
        ("app_is_file", "No selected assembly"),
    ],
)
def test_invalid_input_cannot_produce_successful_inventory(tmp_path, case, message):
    extracted = tmp_path / "extracted"
    version = extracted / "test-version"
    version.mkdir(parents=True)
    app = version / "app"
    if case == "app_is_file":
        app.write_bytes(b"authored fixture")
    else:
        app.mkdir()
        if case != "empty_app":
            (app / "Slingshot.exe").write_bytes(b"authored fixture; never executed")
    archive = tmp_path / "archive"
    if case != "missing_archive":
        archive.mkdir()
        if case != "empty_archive":
            (archive / "fixture.exe").write_bytes(b"authored fixture; never executed")
    evidence = tmp_path / "evidence"
    manifest = tmp_path / "manifest.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--extracted-root", str(extracted),
         "--installer-root", str(archive), "--rea-root", str(tmp_path / "no-rea"),
         "--node", sys.executable, "--evidence-dir", str(evidence),
         "--manifest", str(manifest)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 2
    assert message in result.stderr
    assert not evidence.exists()
    assert not manifest.exists()
