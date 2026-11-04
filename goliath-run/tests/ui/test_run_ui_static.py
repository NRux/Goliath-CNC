# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The interface's static files: offline by construction, no inline code (unit U01; design 8.2, 8.6).

No CDN, web font, analytics or update check: every asset is a file of the
package, read with importlib.resources, and every reference between them is
relative. Nothing inline, so the Content-Security-Policy can forbid inline
script and style (red-team C-09: a shared job file must never be able to
script a page that can arm a 900 W job).
"""
from __future__ import annotations

import fnmatch
import importlib.resources
import re
import tomllib
from html.parser import HTMLParser
from importlib.resources.abc import Traversable
from pathlib import Path
from urllib.parse import urlsplit

from run_ui_harness import Harness

from goliath_run.ui import security as S
from goliath_run.ui import server as U

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Anything that reaches off this machine: a scheme with a host, a protocol-relative URL, or a network scheme.
EXTERNAL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://|(?:src|href|action)\s*=\s*[\"']?\s*//|url\(\s*[\"']?\s*//"
                      r"|@import\s+[\"']?\s*//")


def _static_files() -> list[tuple[str, Traversable]]:
    out: list[tuple[str, Traversable]] = []

    def walk(node: Traversable, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = prefix + child.name
            if child.is_dir():
                walk(child, rel + "/")
            else:
                out.append((rel, child))

    walk(importlib.resources.files("goliath_run.ui").joinpath("static"), "")
    return out


def _text(node: Traversable) -> str:
    return node.read_bytes().decode("utf-8")


def test_static_tree_exists_with_the_shell() -> None:
    names = [rel for rel, _ in _static_files()]
    assert {"index.html", "app.js", "app.css"} <= set(names), names


def test_static_assets_contain_no_external_urls() -> None:
    files = _static_files()
    assert files
    for rel, node in files:
        if Path(rel).suffix not in U.TEXT_SUFFIXES:
            continue
        text = _text(node)
        found = EXTERNAL.findall(text)
        assert not found, "%s refers off this machine: %r" % (rel, found)


def test_static_files_have_known_types_and_names() -> None:
    for rel, node in _static_files():
        assert Path(rel).suffix in U.CONTENT_TYPES, rel
        for part in rel.split("/"):
            assert re.fullmatch(U.STATIC_NAME, part), rel


class _Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.inline_scripts: list[str] = []
        self.styles = 0
        self.bad_attrs: list[tuple[str, str]] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        for name, value in attrs:
            if name.startswith("on") or name == "style":
                self.bad_attrs.append((tag, name))
            if value is not None and value.strip().lower().startswith("javascript:"):
                self.bad_attrs.append((tag, name))
        if tag == "script":
            self._in_script = "src" not in a
            if a.get("src"):
                self.refs.append(a["src"] or "")
        if tag == "style":
            self.styles += 1
        if tag in ("link", "a", "img", "iframe", "source", "audio", "video", "form", "object", "embed"):
            for key in ("href", "src", "action", "data"):
                if a.get(key):
                    self.refs.append(a[key] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts.append(data)


def _html_files() -> list[tuple[str, str]]:
    return [(rel, _text(node)) for rel, node in _static_files() if rel.endswith(".html")]


def test_csp_has_no_unsafe_inline(ui: Harness) -> None:
    for token in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "'strict-dynamic'", "*", "http:",
                  "https:", "blob:", "'wasm-unsafe-eval'"):
        assert token not in S.CSP.split(), token
    directives = dict(d.strip().split(" ", 1) for d in S.CSP.split(";"))
    assert [k for k, v in directives.items() if "data:" in v] == ["img-src"]
    # The header on the wire is that policy.
    r = ui.browser().request("GET", "/")
    assert r.one("Content-Security-Policy") == S.CSP
    # And nothing in the static tree needs inline script or style (the policy would block it).
    for rel, text in _html_files():
        p = _Refs()
        p.feed(text)
        assert p.inline_scripts == [], rel
        assert p.styles == 0, rel
        assert p.bad_attrs == [], (rel, p.bad_attrs)
    for rel, node in _static_files():
        if rel.endswith(".js"):
            js = _text(node)
            for pattern in (r"\beval\s*\(", r"\bnew\s+Function\b", r"\bdocument\.write", r"setTimeout\(\s*[\"'`]",
                            r"setInterval\(\s*[\"'`]", r"javascript:"):
                assert not re.search(pattern, js), (rel, pattern)


def test_every_referenced_asset_is_relative_and_served(ui: Harness) -> None:
    c = ui.browser()
    index = c.request("GET", "/")
    assert index.status == 200 and index.one("Content-Type") == "text/html; charset=utf-8"
    p = _Refs()
    p.feed(index.body.decode("utf-8"))
    assert p.refs, "the shell references its script and style"
    for ref in p.refs:
        parts = urlsplit(ref)
        assert not parts.scheme and not parts.netloc and not ref.startswith("/"), ref
        r = c.request("GET", "/" + parts.path)
        assert r.status == 200, (ref, r.status)
        suffix = Path(parts.path).suffix
        assert r.one("Content-Type") == U.CONTENT_TYPES[suffix], ref
    # Each static file is served as its own bytes.
    for rel, node in _static_files():
        r = c.request("GET", "/static/" + rel)
        assert r.status == 200 and r.body == node.read_bytes(), rel


def test_static_files_are_package_data() -> None:
    # An installed wheel must carry the files: setuptools ships only .py files unless told otherwise.
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as f:
        config = tomllib.load(f)
    package_data = config["tool"]["setuptools"]["package-data"]
    patterns = package_data.get("goliath_run.ui", [])
    for rel, _ in _static_files():
        assert any(fnmatch.fnmatch("static/" + rel, pattern) for pattern in patterns), (rel, patterns)
