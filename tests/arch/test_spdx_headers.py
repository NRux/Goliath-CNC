# SPDX-License-Identifier: GPL-3.0-or-later
"""Every source file carries the SPDX header its folder's licence requires.

The checker is tests/arch/spdx_check.py; CI also runs it on its own (job "SPDX
headers"). Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import spdx_check
from repo_git import REPO, Git, git_unavailable


def _write(root: Path, rel: str, data: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8"))


def test_spdx_headers_present(git: Git) -> None:
    report = spdx_check.check_files(REPO, spdx_check.list_files(REPO))
    assert not report.problems, "SPDX header problems:\n" + "\n".join("  %s: %s" % p for p in report.problems)
    # Positive control: the scan really covered all three packages and the
    # repository-level code, so an empty file list cannot pass unseen.
    for rel in (
        "goliath-link/src/goliath_link/__init__.py",
        "goliath-prep/src/goliath_prep/__init__.py",
        "goliath-run/src/goliath_run/__init__.py",
        "tools/serial_listen.py",
        "tests/arch/astscan.py",
    ):
        assert rel in report.checked, rel


@pytest.mark.parametrize(
    "rel, licence",
    [
        ("goliath-link/src/goliath_link/client.py", "Apache-2.0"),
        ("goliath-link/tests/test_client.py", "Apache-2.0"),
        ("docs/protocol/README.md", "Apache-2.0"),
        ("docs/prep/gi-format.md", "Apache-2.0"),
        ("posts/freecad/goliath_post.py", "Apache-2.0"),
        ("posts/fusion/goliath.cps", "Apache-2.0"),
        ("goliath-prep/src/goliath_prep/__init__.py", "GPL-3.0-or-later"),
        ("goliath-run/src/goliath_run/ui/static/app.js", "GPL-3.0-or-later"),
        ("docs/field-guide/02-first-probe.md", "GPL-3.0-or-later"),
        ("tools/serial_listen.py", "GPL-3.0-or-later"),
        ("tests/arch/astscan.py", "GPL-3.0-or-later"),
        ("setup.py", "GPL-3.0-or-later"),
        # A prefix only counts at a folder boundary.
        ("goliath-link-extras/x.py", "GPL-3.0-or-later"),
        ("posts.py", "GPL-3.0-or-later"),
    ],
)
def test_expected_licence_by_folder(rel: str, licence: str) -> None:
    assert spdx_check.expected_licence(rel) == licence


def test_header_forms_accepted() -> None:
    gpl = "SPDX-License-Identifier: GPL-3.0-or-later"
    assert spdx_check.check_text("tools/a.py", "#!/usr/bin/env python3\n# %s\n" % gpl) is None
    assert spdx_check.check_text("tools/a.py", "﻿# %s\n" % gpl) is None  # UTF-8 byte-order mark
    assert spdx_check.check_text("tools/a.js", "// %s\n" % gpl) is None
    assert spdx_check.check_text("tools/a.css", "/* %s */\n" % gpl) is None
    assert spdx_check.check_text("tools/a.html", "<!doctype html>\n<!-- %s -->\n" % gpl) is None
    assert spdx_check.check_text("tools/a.ps1", "# %s\r\nWrite-Host hi\r\n" % gpl) is None


def test_missing_wrong_and_late_headers_are_problems() -> None:
    missing = spdx_check.check_text("goliath-prep/src/goliath_prep/x.py", '"""No header."""\n')
    assert missing is not None and "missing" in missing and "GPL-3.0-or-later" in missing
    wrong = spdx_check.check_text("goliath-link/src/goliath_link/x.py", "# SPDX-License-Identifier: GPL-3.0-or-later\n")
    assert wrong is not None and "Apache-2.0" in wrong and "GPL-3.0-or-later" in wrong
    late = "\n" * spdx_check.HEADER_LINES + "# SPDX-License-Identifier: GPL-3.0-or-later\n"
    assert spdx_check.check_text("tools/x.py", late) is not None
    two = "# SPDX-License-Identifier: GPL-3.0-or-later\n# SPDX-License-Identifier: Apache-2.0\n"
    assert "more than one" in (spdx_check.check_text("tools/x.py", two) or "")
    assert spdx_check.check_text("tools/empty.py", "") is not None


def test_prose_header_is_optional_but_must_match() -> None:
    assert spdx_check.check_text("docs/protocol/README.md", "# Title\n") is None
    ok = "<!-- SPDX-License-Identifier: Apache-2.0 -->\n# Title\n"
    assert spdx_check.check_text("docs/protocol/README.md", ok) is None
    wrong = "<!-- SPDX-License-Identifier: GPL-3.0-or-later -->\n# Title\n"
    assert spdx_check.check_text("docs/protocol/README.md", wrong) is not None
    # Prose that merely mentions the tag is not a header.
    assert spdx_check.check_text("LICENSING.md", "Files carry an `SPDX-License-Identifier: X` header.\n") is None


def test_files_that_need_no_header_are_not_checked() -> None:
    assert not spdx_check.needs_check("goliath-link/pyproject.toml")
    assert not spdx_check.needs_check("tests/arch/goliath_link_freeze.json")
    assert not spdx_check.needs_check("LICENSE")
    assert spdx_check.needs_check("goliath-run/src/goliath_run/ui/static/index.html")
    assert spdx_check.needs_check("posts/fusion/goliath.cps")


def test_check_files_reports_and_never_reads_the_vendor_folder(tmp_path: Path) -> None:
    _write(tmp_path, "tools/good.py", "# SPDX-License-Identifier: GPL-3.0-or-later\n")
    _write(tmp_path, "tools/bad.py", "print('no header')\n")
    _write(tmp_path, "goliath-link/src/x.py", "# SPDX-License-Identifier: GPL-3.0-or-later\n")
    (tmp_path / "tools" / "latin1.py").write_bytes(b"# caf\xe9\n")
    _write(tmp_path, "old_software/vendor.py", "print('vendor')\n")
    report = spdx_check.check_files(
        tmp_path,
        ["tools/good.py", "tools/bad.py", "goliath-link/src/x.py", "tools/latin1.py", "old_software/vendor.py"],
    )
    problems = dict(report.problems)
    assert set(problems) == {"tools/bad.py", "goliath-link/src/x.py", "tools/latin1.py"}
    assert "UTF-8" in problems["tools/latin1.py"]
    assert "old_software/vendor.py" not in report.checked
    assert "tools/good.py" in report.checked


def test_list_files_uses_git_and_excludes_the_vendor_folder(tmp_path: Path, git: Git) -> None:
    del git  # only needed to skip (or fail) cleanly where git is missing
    _write(tmp_path, "tools/a.py", "# SPDX-License-Identifier: GPL-3.0-or-later\n")
    _write(tmp_path, "tools/new_untracked.py", "# SPDX-License-Identifier: GPL-3.0-or-later\n")
    _write(tmp_path, "old_software/vendor.py", "print('vendor')\n")
    _write(tmp_path, "build/ignored.py", "print('ignored')\n")
    _write(tmp_path, ".gitignore", "build/\n")
    for args in (["init", "-q"], ["add", "tools/a.py", "old_software/vendor.py", ".gitignore"]):
        result = subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True)
        if result.returncode != 0:
            git_unavailable("cannot create a scratch git repository: %s" % result.stderr.strip())
    files = spdx_check.list_files(tmp_path)
    assert "tools/a.py" in files
    assert "tools/new_untracked.py" in files  # checked before it is committed
    assert not any(f.startswith("old_software/") for f in files)
    assert "build/ignored.py" not in files


def test_list_files_outside_a_checkout_is_an_error(tmp_path: Path, git: Git) -> None:
    del git
    with pytest.raises(spdx_check.SpdxCheckError):
        spdx_check.list_files(tmp_path / "not-a-folder")
    # An existing folder that is not the top of a checkout is refused too,
    # whether git finds no checkout or an enclosing one.
    with pytest.raises(spdx_check.SpdxCheckError):
        spdx_check.list_files(tmp_path)


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture, git: Git) -> None:
    del git
    assert spdx_check.main(["--root", str(REPO)]) == 0
    assert "files checked" in capsys.readouterr().out
    _write(tmp_path, "tools/bad.py", "print('no header')\n")
    init = subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), capture_output=True, text=True)
    if init.returncode != 0:
        git_unavailable("cannot create a scratch git repository: %s" % init.stderr.strip())
    assert spdx_check.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "tools/bad.py" in out and "missing" in out
    assert spdx_check.main(["--root", str(tmp_path / "nowhere")]) == 2
