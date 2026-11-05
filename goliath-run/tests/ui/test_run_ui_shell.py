# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The page shell: persistent top bar, banner icons, screens, and no HTML from the session (unit U02; design 8.3-8.7).

The page is vanilla HTML, CSS and JavaScript with no build step. It renders
what the presenters say; these tests read the static files to check what a
browser test would otherwise have to: the top bar says NO SOFTWARE STOP before
any event arrives, every banner icon name has a drawing, session text never
goes through an HTML parser, and the screens of this unit are reachable.
"""
from __future__ import annotations

import importlib.resources
import re
from html.parser import HTMLParser

from goliath_run.types import BANNER_ICONS, BANNER_SEVERITIES
from goliath_run.ui import presenters as P


def _static(name: str) -> str:
    return importlib.resources.files("goliath_run.ui").joinpath("static", name).read_bytes().decode("utf-8")


class _Elements(HTMLParser):
    """Collects every element with its attributes, and the text directly inside elements with an id."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: dict[str, str] = {}
        self._open: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.elements.append((tag, a))
        if tag not in ("meta", "link", "use", "path", "circle", "rect", "polygon", "line", "br", "input"):
            self._open.append(a.get("id"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag not in ("meta", "link", "use", "path", "circle", "rect", "polygon", "line", "br", "input") and \
                self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        for ident in self._open:
            if ident:
                self.text[ident] = self.text.get(ident, "") + data


def _index() -> _Elements:
    p = _Elements()
    p.feed(_static("index.html"))
    return p


def test_top_bar_says_no_software_stop_before_any_event() -> None:
    # The fixed red chip is in the page itself, so it shows before the first event and when events stop.
    page = _index()
    assert page.text["stop-chip"].strip() == P.NO_SOFTWARE_STOP_CHIP
    assert "FIRMWARE" in page.text["firmware"] or "waiting" in page.text["firmware"]


def test_every_banner_icon_has_a_drawing() -> None:
    # Design 8.4/8.7: colour is never the only signal. Every icon name a banner may carry is drawn.
    symbols = {a.get("id") for tag, a in _index().elements if tag == "symbol"}
    for name in BANNER_ICONS:
        assert "icon-%s" % name in symbols, name


def test_every_severity_has_a_style() -> None:
    css = _static("app.css")
    for severity in BANNER_SEVERITIES:
        assert ".severity-%s" % severity in css, severity


def test_session_text_never_goes_through_an_html_parser() -> None:
    # Red-team C-09: a job file must never script this page. Text goes in through textContent only.
    for name in ("app.js", "screens.js"):
        js = _static(name)
        for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write",
                        r"createContextualFragment", r"DOMParser", r"\.srcdoc\b"):
            assert not re.search(pattern, js), (name, pattern)


def test_unit_screens_are_reachable_from_the_page() -> None:
    page = _index()
    links = {a.get("href") for tag, a in page.elements if tag == "a"}
    for name in P.SCREENS:
        assert "#%s" % name in links, name
    scripts = [a.get("src") for tag, a in page.elements if tag == "script"]
    assert scripts == ["static/app.js", "static/screens.js"]
    js = _static("screens.js")
    assert "api/screen/" in js


def test_page_uses_the_presenters_stale_texts() -> None:
    # A view that stops updating falls back to what the presenters say for that case: the robot may be moving,
    # and no software stop is offered.
    js = _static("app.js")
    assert "if_stale" in js
    assert 'addEventListener("view"' in js


def test_targets_are_large_enough() -> None:
    # Design 8.7: touch targets of at least 44 px.
    css = _static("app.css")
    for selector in ("button", ".nav a", ".chip"):
        block = re.search(re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}", css)
        assert block, selector
        assert "min-height: 44px" in block.group(1), selector
