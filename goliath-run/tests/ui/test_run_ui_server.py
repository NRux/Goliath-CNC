# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The local interface server over real loopback sockets (unit U01; the project design, section 8.2).

A server that can start a 900 W job is a target for cross-site requests and
DNS rebinding from any page the laptop visits. These tests send what such a
page (or a confused browser) would send and check that nothing gets through:
the Host check, the per-launch cookie from a one-time link, the Origin check
on every POST, the security headers on every response, no CORS, and the
single controlling tab.
"""
from __future__ import annotations

import importlib.resources
import inspect
import json
import logging
import re
import socket
import threading
import time
from typing import Any

import pytest
from run_ui_harness import Client, EventReader, Harness, csp_directives

from goliath_run.types import Arm, Connect, IStoppedTheRobot
from goliath_run.ui import UIServer, serve
from goliath_run.ui import security as S
from goliath_run.ui import server as U
from goliath_run.ui.sse import HEARTBEAT_S

LOGGER = "goliath_run.ui"

AUTHENTICATED_GETS = ("/", "/api/snapshot", "/static/index.html", "/static/app.js", "/static/app.css")
POSTS = ("/api/intent", "/api/lease")


def _raw(port: int, data: bytes, timeout: float = 5) -> bytes:
    """Send raw bytes and read the whole reply (the server closes after one response)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def _raw_headers(reply: bytes) -> list[tuple[str, str]]:
    head = reply.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")[1:]
    return [(k.strip(), v.strip()) for k, v in (line.split(":", 1) for line in head if line)]


# ---------------------------------------------------------------------------
# Host header (DNS rebinding)
# ---------------------------------------------------------------------------


def test_bad_host_header_rejected(ui: Harness) -> None:
    c = ui.browser()
    port = ui.port
    other = port + 1 if port < 65535 else port - 1
    bad = [
        "evil.example",
        "evil.example:%d" % port,
        "127.0.0.1",
        "localhost",
        "127.0.0.1:%d" % other,
        "localhost:%d" % other,
        "[::1]:%d" % port,
        "0.0.0.0:%d" % port,
        "127.0.0.2:%d" % port,
        "localhost.:%d" % port,
        "127.0.0.1:0%d" % port,
        "127.0.0.1:+%d" % port,
        "127.0.0.1:%d.evil.example" % port,
        "evil.example.127.0.0.1:%d" % port,
        "127.0.0.1:%d@evil.example" % port,
        "user@127.0.0.1:%d" % port,
        "127.0.0.1:%d:%d" % (port, port),
        "",
    ]
    for path in AUTHENTICATED_GETS + ("/launch?code=x", "/nothing-here"):
        for host in bad:
            r = c.request("GET", path, host=host)
            assert r.status == 403, (path, host, r.status)
            assert b"snapshot" not in r.body and b"<script" not in r.body, (path, host)
        # No Host header at all, and two of them (even two good ones: ambiguous).
        assert c.request("GET", path, host=None).status == 403
        assert c.request("GET", path, host=[c.host, "evil.example"]).status == 403
        assert c.request("GET", path, host=[c.host, c.host]).status == 403
    for path in POSTS:
        r = c.post_json(path, {"action": "acquire", "lease": None}, host="evil.example:%d" % port)
        assert r.status == 403
    # The event stream too.
    for host in ("evil.example:%d" % port, "127.0.0.1"):
        reader = EventReader(port, [("Host", host), ("Cookie", c.cookie or "")])
        try:
            assert reader.status == 403
        finally:
            reader.close()
    assert ui.sink.intents == []

    # The two names of this origin pass the check (the hostname is case-insensitive).
    for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "LOCALHOST:%d" % port):
        assert c.request("GET", "/api/snapshot", host=host).status == 200, host


def test_bad_host_does_not_use_up_the_launch_link(ui: Harness) -> None:
    c = ui.browser(signed_in=False)
    path = ui.server.url.split(str(ui.port), 1)[1]
    assert c.request("GET", path, host="evil.example:%d" % ui.port, cookie=None).status == 403
    assert c.sign_in().status == 200


# ---------------------------------------------------------------------------
# Per-launch token: one-time link, then an HttpOnly SameSite=Strict cookie
# ---------------------------------------------------------------------------


def test_token_cookie_required(ui: Harness) -> None:
    anonymous = ui.browser(signed_in=False)
    for path in AUTHENTICATED_GETS:
        r = anonymous.request("GET", path)
        assert r.status == 401, (path, r.status)
        assert b"TEST-BANNER" not in r.body and b"<script" not in r.body
    for path in POSTS:
        r = anonymous.post_json(path, {"action": "acquire", "lease": None})
        assert r.status == 401, path
    reader = anonymous.events()
    try:
        assert reader.status == 401
    finally:
        reader.close()

    c = ui.browser()
    name, token = c.cookie.split("=", 1)
    assert name == S.cookie_name(ui.port) == "goliath_ui_%d" % ui.port
    wrong = [
        "%s=%s" % (name, "x" * len(token)),
        "%s=%s" % (name, token[:-1]),
        "%s=%s" % (name, ""),
        "%s=%s" % ("goliath_ui_%d" % (ui.port + 1), token),  # the right token under another launch's name
        "%s=%s; %s=%s" % (name, token, name, "x" * len(token)),  # two values for our name: ambiguous
        "%s=%s; %s=%s" % (name, "x" * len(token), name, token),
        token,
    ]
    for cookie in wrong:
        for path in AUTHENTICATED_GETS:
            assert c.request("GET", path, cookie=cookie).status == 401, (cookie, path)
        assert c.post_json("/api/lease", {"action": "acquire", "lease": None}, cookie=cookie).status == 401
    # Two Cookie headers carrying our name are ambiguous too.
    assert c.request("GET", "/api/snapshot", cookie=[c.cookie, "%s=%s" % (name, "x" * len(token))]).status == 401
    for path in AUTHENTICATED_GETS:
        assert c.request("GET", path).status == 200, path
    # Other cookies next to ours do not matter.
    assert c.request("GET", "/api/snapshot", cookie="other=1; %s; theme=dark" % c.cookie).status == 200
    assert ui.sink.intents == []


def test_launch_link_sets_a_strict_httponly_session_cookie(ui: Harness) -> None:
    c = ui.browser(signed_in=False)
    r = c.sign_in()
    assert r.status == 200
    (set_cookie,) = r.header("Set-Cookie")
    parts = [p.strip() for p in set_cookie.split(";")]
    attrs = {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else None) for p in parts[1:]}
    assert "httponly" in attrs
    assert attrs.get("samesite") == "Strict"
    assert attrs.get("path") == "/"
    # Host-only (no Domain), and a session cookie: it dies with the browser session.
    assert "domain" not in attrs and "expires" not in attrs and "max-age" not in attrs
    # The token is not the launch code: the link in the browser's history is not the credential.
    code = ui.server.url.rsplit("=", 1)[1]
    token = parts[0].split("=", 1)[1]
    assert code != token and code not in r.body.decode("utf-8") and token not in r.body.decode("utf-8")
    assert len(token) >= 40 and len(code) >= 40  # 256 random bits, base64url
    # The sign-in page moves on to the interface without a script (the CSP allows none inline).
    assert re.search(rb'<meta http-equiv="refresh" content="0; url=/">', r.body)
    assert b"<script" not in r.body
    assert r.one("Content-Type") == "text/html; charset=utf-8"


def test_launch_link_works_once(ui: Harness) -> None:
    first = ui.browser()
    other = ui.browser(signed_in=False)
    r = other.sign_in()
    assert r.status == 403 and not r.header("Set-Cookie") and other.cookie is None
    assert other.request("GET", "/api/snapshot").status == 401
    # The browser that holds the cookie can open the used link again and is sent on to the interface.
    r = first.request("GET", ui.server.url.split(str(ui.port), 1)[1])
    assert r.status == 200 and not r.header("Set-Cookie")
    # A new one-time link (the command line can issue one) signs in another browser, once.
    url = ui.server.new_launch_url()
    assert url != ui.server.url and url.startswith("http://127.0.0.1:%d/launch?code=" % ui.port)
    second = ui.browser(launch_url=url)
    assert second.request("GET", "/api/snapshot").status == 200
    assert ui.browser(signed_in=False, launch_url=url).sign_in().status == 403


@pytest.mark.parametrize("query", ["", "?code=", "?code=abc", "?code=%s&code=%s", "?code=%s&x=1", "?x=%s",
                                   "?code=%s%%00", "?CODE=%s"])
def test_malformed_launch_link_refused_and_does_not_burn_the_code(ui: Harness, query: str) -> None:
    code = ui.server.url.rsplit("=", 1)[1]
    c = ui.browser(signed_in=False)
    q = query.replace("%s", code) if "%s" in query else query
    r = c.request("GET", "/launch" + q, cookie=None)
    assert r.status == 403 and not r.header("Set-Cookie")
    assert c.sign_in().status == 200


def test_each_launch_has_its_own_token(source: Any, sink: Any) -> None:
    with serve(source, sink) as a, serve(source, sink) as b:
        ca = Client(a.port, a.url)
        cb = Client(b.port, b.url)
        ca.sign_in()
        cb.sign_in()
        token_a = ca.cookie.split("=", 1)[1]
        token_b = cb.cookie.split("=", 1)[1]
        assert token_a != token_b
        # Cookies are not separated by port in browsers: the name carries the port, and a's token is useless on b.
        assert cb.request("GET", "/api/snapshot", cookie="%s=%s" % (S.cookie_name(b.port), token_a)).status == 401


def test_launch_code_and_token_never_logged(source: Any, sink: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with serve(source, sink) as server:
        c = Client(server.port, server.url)
        c.sign_in()
        c.request("GET", "/api/snapshot")
        c.request("GET", server.url.split(str(server.port), 1)[1], cookie=None)  # a refused second use
        code = server.url.rsplit("=", 1)[1]
        token = c.cookie.split("=", 1)[1]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/launch" in text  # the requests are logged...
    assert code not in text and token not in text  # ...without the secrets


# ---------------------------------------------------------------------------
# Origin on every POST; JSON only
# ---------------------------------------------------------------------------


def test_origin_checked_on_post(ui: Harness) -> None:
    c = ui.browser()
    port = ui.port
    bad_origins: list[Any] = [
        None,
        "null",
        "http://evil.example",
        "http://127.0.0.1:%d" % (port + 1),
        "https://127.0.0.1:%d" % port,
        "http://127.0.0.1",
        "http://127.0.0.1:%d/" % port,
        "http://localhost:%d" % port,  # the request's Host is 127.0.0.1: another origin
        "http://127.0.0.1:%d.evil.example" % port,
        "HTTP://127.0.0.1:%d " % port,
        ["http://127.0.0.1:%d" % port, "http://127.0.0.1:%d" % port],
        "",
    ]
    bodies = {"/api/lease": {"action": "acquire", "lease": None},
              "/api/intent": {"lease": "x", "intent": {"type": "Connect"}}}
    for path in POSTS:
        for origin in bad_origins:
            r = c.post_json(path, bodies[path], origin=origin)
            assert r.status == 403, (path, origin, r.status)
        for site in ("cross-site", "same-site", "none", "", ["same-origin", "same-origin"]):
            r = c.post_json(path, bodies[path], fetch_site=site)
            assert r.status == 403, (path, site, r.status)
    assert c.acquire().status == 200
    for ctype in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                  "application/jsonx", "text/json"):
        r = c.post_json("/api/intent", {"lease": c.lease, "intent": {"type": "Connect"}}, content_type=ctype)
        assert r.status == 415, (ctype, r.status)
    assert ui.sink.intents == []
    # The same request from this origin passes, with or without Sec-Fetch-Site (older browsers do not send it).
    assert c.submit({"type": "Connect"}).status == 202
    r = c.post_json("/api/intent", {"lease": c.lease, "intent": {"type": "Connect"}}, fetch_site=None,
                    content_type="application/json; charset=utf-8")
    assert r.status == 202
    assert ui.sink.intents == [Connect(), Connect()]
    # A page served as http://localhost:<port> posts with its own Origin and Host.
    r = c.post_json("/api/intent", {"lease": c.lease, "intent": {"type": "Connect"}},
                    host="localhost:%d" % port, origin="http://localhost:%d" % port)
    assert r.status == 202



def test_cross_origin_reads_refused_by_fetch_metadata(ui: Harness) -> None:
    c = ui.browser()
    for site in ("cross-site", "same-site"):
        assert c.request("GET", "/api/snapshot", fetch_site=site).status == 403
        reader = c.events(extra=[("Sec-Fetch-Site", site)])
        try:
            assert reader.status == 403
        finally:
            reader.close()
    # A typed address or bookmark ("none") opens the page itself.
    assert c.request("GET", "/", fetch_site="none").status == 200


def test_state_changes_are_post_only(ui: Harness) -> None:
    c = ui.browser()
    assert c.acquire().status == 200
    for path in POSTS:
        r = c.request("GET", path)
        assert r.status == 405 and r.one("Allow") == "POST", path
    for path in ("/", "/api/snapshot", "/events", "/static/app.js"):
        r = c.post_json(path, {})
        assert r.status == 405 and r.one("Allow") == "GET", path
    for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"):
        r = c.request(method, "/api/intent")
        assert r.status in (405, 501), (method, r.status)
    assert ui.sink.intents == []


# ---------------------------------------------------------------------------
# Security headers on every response; no CORS
# ---------------------------------------------------------------------------


def _every_kind_of_response(ui: Harness) -> list[tuple[str, list[tuple[str, str]]]]:
    c = ui.browser()
    anon = ui.browser(signed_in=False)
    out: list[tuple[str, list[tuple[str, str]]]] = []

    def keep(label: str, r: Any) -> None:
        out.append((label, r.headers))

    keep("launch", ui.browser(launch_url=ui.server.new_launch_url()).seen[-1])
    keep("launch refused", anon.request("GET", "/launch?code=nope", cookie=None))
    for path in AUTHENTICATED_GETS:
        keep("200 " + path, c.request("GET", path))
    keep("401", anon.request("GET", "/"))
    keep("403 host", c.request("GET", "/", host="evil.example"))
    keep("403 origin", c.post_json("/api/lease", {"action": "acquire", "lease": None}, origin="http://evil.example"))
    keep("404", c.request("GET", "/no-such-thing"))
    keep("405", c.request("GET", "/api/intent"))
    keep("415", c.post_json("/api/lease", {}, content_type="text/plain"))
    keep("lease", c.acquire())
    keep("202", c.submit({"type": "Connect"}))
    keep("400", c.submit({"type": "NoSuchIntent"}))
    keep("options", c.request("OPTIONS", "/api/intent", origin="http://evil.example",
                              headers=[("Access-Control-Request-Method", "POST")]))
    reader = c.events()
    try:
        out.append(("events", reader.headers))
    finally:
        reader.close()
    many = b"".join(b"X-H%d: 1\r\n" % i for i in range(101))
    out.append(("431 raw", _raw_headers(_raw(ui.port, b"GET / HTTP/1.1\r\n" + many + b"\r\n"))))
    out.append(("501 raw", _raw_headers(_raw(ui.port, b"BREW / HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % ui.port))))
    return out


def test_http_0_9_request_gets_only_an_error_text(ui: Harness) -> None:
    # HTTP/0.9 has no headers at all (no browser still speaks it); such a request gets an error and nothing else.
    # The standard library answers a header-less request line in one of two ways, depending on its release
    # (3.11.18 and 3.14.6 a bare "400 ..." line, 3.13.18 a full HTTP/1.0 response): either is fine, as long as
    # it is a 400 with no session data, and a response with headers carries the security headers.
    reply = _raw(ui.port, b"GARBAGE\r\n\r\n")
    assert reply.startswith((b"400 ", b"HTTP/1.0 400 ")), reply[:80]
    assert b"seq" not in reply and b"<script" not in reply
    if reply.startswith(b"HTTP/"):
        assert ("Content-Security-Policy", S.CSP) in _raw_headers(reply)
    reply = _raw(ui.port, b"GET /api/snapshot\r\n\r\n")
    assert b"seq" not in reply and b"TEST-BANNER" not in reply
    assert not reply.startswith((b"HTTP/1.0 200", b"HTTP/1.1 200")), reply[:80]


def test_csp_header(ui: Harness) -> None:
    # Parsed, not searched as a substring: "default-src 'self' https:" contains "default-src 'self'".
    directives = csp_directives(S.CSP)
    assert directives["default-src"] == ["'self'"]
    assert directives["img-src"] == ["'self'", "data:"]
    assert directives["frame-ancestors"] == ["'none'"]
    for label, headers in _every_kind_of_response(ui):
        values = [v for k, v in headers if k.lower() == "content-security-policy"]
        assert values == [S.CSP], (label, headers)
        names = {k.lower(): v for k, v in headers}
        assert names.get("x-content-type-options") == "nosniff", label
        assert names.get("cache-control") == "no-store", label
        assert names.get("referrer-policy") == "no-referrer", label
        assert names.get("x-frame-options") == "DENY", label
        assert names.get("cross-origin-resource-policy") == "same-origin", label


def test_no_cors_headers(ui: Harness) -> None:
    for label, headers in _every_kind_of_response(ui):
        cors = [k for k, _ in headers if k.lower().startswith("access-control-")]
        assert cors == [], (label, cors)
    c = ui.second_browser()
    r = c.request("OPTIONS", "/api/intent", origin="http://evil.example",
                  headers=[("Access-Control-Request-Method", "POST"),
                           ("Access-Control-Request-Headers", "content-type")])
    assert r.status == 405  # a preflight is never approved
    r = c.request("GET", "/api/snapshot", origin="http://evil.example")
    assert not [k for k, _ in r.headers if k.lower().startswith("access-control-")]


def test_server_header_names_no_versions(ui: Harness) -> None:
    r = ui.browser().request("GET", "/api/snapshot")
    assert r.one("Server") == "goliath-run"


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_binds_loopback_only(ui: Harness) -> None:
    assert ui.server.bound_address == ("127.0.0.1", ui.port)
    assert ui.server.origin == "http://127.0.0.1:%d" % ui.port
    assert ui.server.url.startswith("http://127.0.0.1:%d/launch?code=" % ui.port)
    # There is no way to ask for another address.
    names = set(inspect.signature(serve).parameters)
    assert not names & {"host", "address", "bind", "interface", "addr"}, names
    # Another loopback address of this machine is not served (127.0.0.0/8 is loopback on Linux and Windows).
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.2", ui.port), timeout=3).close()


def _lan_address() -> str | None:
    """A non-loopback IPv4 address of this machine, or None (no packet is sent: UDP connect only routes)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1 (RFC 5737)
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def test_not_reachable_on_a_network_address(ui: Harness) -> None:
    address = _lan_address()
    if address is None:
        pytest.skip("this machine has no route-able non-loopback IPv4 address to try (environment, not the server)")
    with pytest.raises(OSError):
        socket.create_connection((address, ui.port), timeout=3).close()


def test_port_cannot_be_taken_over(ui: Harness) -> None:
    # Windows lets a second socket with SO_REUSEADDR bind a port in use unless the owner holds
    # SO_EXCLUSIVEADDRUSE; Linux refuses a second bind to a listening port.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            s.bind(("127.0.0.1", ui.port))
    finally:
        s.close()


def test_socket_options_per_platform() -> None:
    assert S.socket_options("nt") == S.SocketOptions(reuse_address=False, exclusive_address=True)
    assert S.socket_options("posix") == S.SocketOptions(reuse_address=False, exclusive_address=False)


def test_serve_refuses_what_is_not_a_source_or_sink(source: Any, sink: Any) -> None:
    with pytest.raises(TypeError, match="SnapshotSource"):
        serve(object(), sink)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="IntentSink"):
        serve(source, object())  # type: ignore[arg-type]
    for port in (-1, 65536, True, "8080", 1.5):
        with pytest.raises((TypeError, ValueError)):
            serve(source, sink, port=port)  # type: ignore[arg-type]
    for hb in (0, -1, float("nan"), float("inf"), 3600):
        with pytest.raises((TypeError, ValueError)):
            serve(source, sink, heartbeat_s=hb)


def test_heartbeat_interval_cannot_exceed_what_the_page_assumes(source: Any, sink: Any) -> None:
    # The page calls a stream stale after three missed heartbeats of the default interval; a longer interval
    # would make a healthy stream look dead, so it is refused rather than accepted.
    assert U.MAX_HEARTBEAT_S == HEARTBEAT_S
    with pytest.raises(ValueError, match="heartbeat_s"):
        serve(source, sink, heartbeat_s=HEARTBEAT_S + 0.5)
    with serve(source, sink, heartbeat_s=HEARTBEAT_S):
        pass
    app_js = (importlib.resources.files("goliath_run.ui") / "static" / "app.js").read_text(encoding="utf-8")
    stale = re.search(r"const STALE_MS = ([0-9]+);", app_js)
    assert stale is not None and int(stale.group(1)) == 3 * int(HEARTBEAT_S * 1000)


def test_port_80_refused_because_browsers_leave_it_out_of_the_host(source: Any, sink: Any) -> None:
    # A browser never writes a URL's default port: for http://127.0.0.1:80/ it sends "Host: 127.0.0.1" and
    # "Origin: http://127.0.0.1", which the Host check (exactly 127.0.0.1:<port>) refuses. On port 80 every
    # request would be refused, so serve() refuses the port up front, with the reason, before it binds anything.
    with pytest.raises(ValueError, match="port 80"):
        serve(source, sink, port=80)


def test_port_in_use_is_a_clear_error(ui: Harness, source: Any, sink: Any) -> None:
    with pytest.raises(OSError, match="127.0.0.1:%d" % ui.port):
        serve(source, sink, port=ui.port)


def test_close_frees_the_port_and_ends_event_streams(source: Any, sink: Any) -> None:
    server = serve(source, sink)
    assert isinstance(server, UIServer)
    c = Client(server.port, server.url)
    c.sign_in()
    reader = c.events()
    try:
        assert reader.snapshots(1)[0].id == "0"
        started = time.monotonic()
        server.close()  # the source is blocked waiting for a snapshot that never comes
        assert time.monotonic() - started < 5
        assert reader.at_eof(timeout=5)
    finally:
        reader.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", server.port), timeout=3).close()
    server.close()  # idempotent


def test_close_does_not_wait_for_an_idle_connection(source: Any, sink: Any) -> None:
    server = serve(source, sink)
    idle = socket.create_connection(("127.0.0.1", server.port), timeout=5)  # sends nothing
    try:
        time.sleep(0.2)
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 5
    finally:
        idle.close()


def test_close_does_not_wait_for_an_idle_connection_where_shutdown_does_not_wake_a_read(
        source: Any, sink: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows, shutting a socket down from another thread does not wake a read blocked on it (the
    # windows-latest CI job saw close() wait its full 5 s). Model that here: close() must still return
    # promptly, because an idle connection is waiting in short steps that watch for the server closing.
    monkeypatch.setattr(U._Server, "drop_connections", lambda self: list(self._conns))
    server = serve(source, sink)
    idle = socket.create_connection(("127.0.0.1", server.port), timeout=5)  # sends nothing
    try:
        time.sleep(0.2)
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 2
    finally:
        idle.close()


def test_a_connection_that_never_sends_a_request_is_closed_after_the_request_timeout(
        source: Any, sink: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(U, "REQUEST_TIMEOUT_S", 0.5)
    server = serve(source, sink)
    idle = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        idle.settimeout(5)
        assert idle.recv(1) == b""  # the server closed it, it did not hang
    finally:
        idle.close()
        server.close()


# ---------------------------------------------------------------------------
# The single controlling tab
# ---------------------------------------------------------------------------


def test_single_controlling_lease(ui: Harness) -> None:
    a = ui.browser()
    b = Client(ui.port, ui.server.url, cookie=a.cookie)  # a second tab of the same browser
    connect = {"type": "Connect"}

    # Nobody controls yet: every tab is read-only.
    assert a.submit(connect).status == 409
    r = a.acquire()
    assert r.status == 200
    grant = r.json()
    assert set(grant) == {"lease", "epoch"} and len(grant["lease"]) >= 40

    # A second tab cannot acquire, and cannot act, with no lease or a made-up one.
    r = b.acquire()
    assert r.status == 409 and b.lease is None
    assert r.json()["state"] == {"epoch": grant["epoch"], "held": True}
    assert b.submit(connect).status == 409
    assert b.submit(connect, lease="x" * 43).status == 409
    assert b.submit(connect, lease=None).status == 409
    assert ui.sink.intents == []

    # The controlling tab acts; acquiring again with its own lease is harmless.
    assert a.submit(connect).status == 202
    assert ui.sink.intents == [Connect()]
    again = a.acquire()
    assert again.status == 200 and again.json() == grant

    # The other tab takes control explicitly; the first one becomes read-only.
    r = b.acquire("take")
    assert r.status == 200 and b.lease not in (None, a.lease)
    assert r.json()["epoch"] > grant["epoch"]
    r = a.submit(connect)
    assert r.status == 409 and r.json()["state"]["held"] is True
    assert a.acquire().status == 409  # its old lease no longer counts
    assert b.submit({"type": "IStoppedTheRobot", "text": "I pressed the stop"}).status == 202
    assert ui.sink.intents == [Connect(), IStoppedTheRobot("I pressed the stop")]

    # Release: only by the holder.
    assert a.post_json("/api/lease", {"action": "release", "lease": a.lease}).status == 409
    assert b.post_json("/api/lease", {"action": "release", "lease": b.lease}).status == 200
    assert b.submit(connect).status == 409
    a.lease = None
    assert a.acquire().status == 200
    assert a.submit(connect).status == 202


def test_lease_requests_are_strict(ui: Harness) -> None:
    c = ui.browser()
    for body in ({}, {"action": "grab", "lease": None}, {"action": "acquire", "lease": None, "x": 1},
                 {"action": "acquire", "lease": 5}, {"action": 1}, [], "acquire", None,
                 {"action": "release"}):
        r = c.post_json("/api/lease", body)
        assert r.status == 400, (body, r.status)
        assert "error" in r.json()


def test_lease_changes_reach_every_tab(ui: Harness) -> None:
    a = ui.browser()
    b = Client(ui.port, ui.server.url, cookie=a.cookie)
    reader = b.events()
    try:
        first = reader.next(skip=("heartbeat", "snapshot"))
        assert first.event == "lease" and first.id is None
        assert first.json() == {"epoch": 0, "held": False}
        a.acquire()
        e = reader.next(skip=("heartbeat", "snapshot"))
        assert e.event == "lease" and e.json() == {"epoch": 1, "held": True}
        b.acquire("take")
        e = reader.next(skip=("heartbeat", "snapshot"))
        assert e.json() == {"epoch": 2, "held": True}
        b.post_json("/api/lease", {"action": "release", "lease": b.lease})
        e = reader.next(skip=("heartbeat", "snapshot"))
        assert e.json() == {"epoch": 3, "held": False}
    finally:
        reader.close()


def test_take_and_submit_cannot_interleave(ui: Harness) -> None:
    # Whatever the timing, an intent is accepted only from the tab that held the lease when it was submitted:
    # every accepted intent was sent under the lease that was current at that moment.
    a = ui.browser()
    b = Client(ui.port, ui.server.url, cookie=a.cookie)
    a.acquire()
    results: list[int] = []
    stop = threading.Event()

    def spam() -> None:
        while not stop.is_set():
            results.append(a.submit({"type": "Cancel"}).status)

    t = threading.Thread(target=spam)
    t.start()
    try:
        time.sleep(0.1)
        assert b.acquire("take").status == 200
        accepted_before = len(ui.sink.intents)
        time.sleep(0.2)
    finally:
        stop.set()
        t.join(10)
    assert len(ui.sink.intents) == accepted_before  # nothing from a after the take returned
    assert set(results) <= {202, 409} and 409 in results


# ---------------------------------------------------------------------------
# Intents and snapshots
# ---------------------------------------------------------------------------


def test_intent_submitted_verbatim(ui: Harness) -> None:
    c = ui.browser()
    c.acquire()
    phrase = "  START 1a2b <b>&amp;é "
    r = c.submit({"type": "Arm", "phrase": phrase})
    # 202 means queued for the session, not accepted by it: the session checks the phrase and answers in a
    # later snapshot. The reply must not read as a yes to an arming whose outcome is not known yet.
    assert r.status == 202 and r.json() == {"queued": "Arm"}
    assert "accept" not in r.body.decode("utf-8").lower()
    assert ui.sink.intents == [Arm(phrase)]


@pytest.mark.parametrize("body,status", [
    (b"{", 400),
    (b"[]", 400),
    (b"\xff\xfe", 400),
    (b'{"lease": "L", "intent": {"type": "Connect"}, "lease": "M"}', 400),  # a key twice: ambiguous
    (b'{"lease": "L", "intent": {"type": "EnterMeasurement", "fact_id": "a.b", "value": NaN, "unit": "mm",'
     b' "tolerance": 1, "operator": "x"}}', 400),
    (b'{"lease": "L"}', 400),
    (b'{"lease": "L", "intent": {"type": "Connect"}, "extra": 1}', 400),
    (b'{"lease": "L", "intent": {"type": "NoSuchIntent"}}', 400),
    (b'{"lease": "L", "intent": {"type": "Arm"}}', 400),
    (b'{"lease": "L", "intent": {"type": "Arm", "phrase": "x", "more": 1}}', 400),
    (b'{"lease": "L", "intent": "Connect"}', 400),
])
def test_bad_intent_requests_refused(ui: Harness, body: bytes, status: int) -> None:
    c = ui.browser()
    c.acquire()
    r = c.request("POST", "/api/intent", body=body.replace(b'"L"', json.dumps(c.lease).encode()))
    assert r.status == status, r.body
    assert r.json()["error"]
    assert ui.sink.intents == []


def test_deeply_nested_body_is_a_bad_request(ui: Harness, caplog: pytest.LogCaptureFixture) -> None:
    # A body nested deeper than the JSON reader can follow is the request's fault: 400 with a reason, not
    # a 500 "internal error" with a traceback in the log that blames goliath-run.
    caplog.set_level(logging.ERROR, logger=LOGGER)
    c = ui.browser()
    c.acquire()
    for body in (b"[" * 30_000 + b"]" * 30_000, b'{"a":' * 10_000 + b"1" + b"}" * 10_000):
        assert len(body) <= U.MAX_BODY
        r = c.request("POST", "/api/intent", body=body)
        assert r.status == 400, (r.status, r.body[:200])
        assert r.json()["error"]
    assert ui.sink.intents == []
    assert [rec.getMessage() for rec in caplog.records] == []


def test_control_characters_never_reach_the_log(source: Any, sink: Any, caplog: pytest.LogCaptureFixture) -> None:
    # Any local program can put terminal control sequences (ESC, BEL, CSI) into a request line or a header.
    # The log shows them escaped, as the standard library's own request log does, so a console that shows
    # the goliath-run log cannot be re-titled, cleared or re-coloured by a request.
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with serve(source, sink) as server:
        port = server.port
        good_host = b"Host: 127.0.0.1:%d\r\n" % port
        _raw(port, b"GET /\x1b]0;owned\x07\x1b[2J HTTP/1.1\r\n" + good_host + b"\r\n")  # 401: no cookie
        _raw(port, b"GET /x\x1b[31m HTTP/1.1\r\nHost: evil\x1b[2J.example\r\n\r\n")  # 403: Host
        c = Client(port, server.url)
        c.sign_in()
        cookie = b"Cookie: " + (c.cookie or "").encode("ascii") + b"\r\n"
        _raw(port, b"GET /nothing\x9b31m\x7f HTTP/1.1\r\n" + good_host + cookie + b"\r\n")  # 404
        _raw(port, b"POST /api/lease\x1b[1m HTTP/1.1\r\n" + good_host + b"Origin: http://evil\x1b[5m\r\n"
                   b"Content-Length: 0\r\n\r\n")  # 403: Origin
    messages = [r.getMessage() for r in caplog.records]
    assert any("owned" in m for m in messages) and any("nothing" in m for m in messages)  # logged...
    for m in messages:  # ...with every C0 and C1 control character escaped
        bad = sorted({"%#04x" % ord(ch) for ch in m if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F})
        assert bad == [], (bad, m)


def test_request_body_limits(ui: Harness) -> None:
    c = ui.browser()
    c.acquire()
    big = json.dumps({"lease": c.lease, "intent": {"type": "Arm", "phrase": "x" * 70_000}}).encode()
    assert c.request("POST", "/api/intent", body=big).status == 413
    # No Content-Length (a chunked body, for example) is refused, not read until the client stops.
    assert c.request("POST", "/api/intent").status == 411
    r = c.request("POST", "/api/intent", headers=[("Transfer-Encoding", "chunked")], body=b"0\r\n\r\n")
    assert r.status in (400, 411, 501)
    assert c.request("POST", "/api/intent", headers=[("Content-Length", "-1")]).status == 400
    assert ui.sink.intents == []


def test_sink_failure_is_reported_and_logged(ui: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER)
    ui.sink.fail = RuntimeError("queue closed")
    c = ui.browser()
    c.acquire()
    r = c.submit({"type": "Connect"})
    assert r.status == 500
    assert "queue closed" in r.json()["error"]
    assert any("queue closed" in (rec.exc_text or "") + rec.getMessage() for rec in caplog.records)


def test_snapshot_endpoint(ui: Harness) -> None:
    c = ui.browser()
    snap = ui.source.publish(firmware="01.02.00", firmware_source="INFO;34")
    r = c.request("GET", "/api/snapshot")
    assert r.status == 200 and r.one("Content-Type") == "application/json; charset=utf-8"
    assert r.json() == snap.to_json()


def test_snapshot_source_failure_is_reported_and_logged(ui: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER)
    ui.source.fail_snapshot = RuntimeError("driver gone")
    r = ui.browser().request("GET", "/api/snapshot")
    assert r.status == 500 and "driver gone" in r.json()["error"]
    assert any("driver gone" in (rec.exc_text or "") + rec.getMessage() for rec in caplog.records)


def test_paths_outside_the_static_tree_refused(ui: Harness) -> None:
    c = ui.browser()
    for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py",
                 "/static//etc/passwd", "/static/", "/static", "/static/.hidden", "/static/a/../index.html",
                 "/static/index.html/", "/static/INDEX.HTML", "/static/index.html%00.js", "/static/\\index.html",
                 "/../goliath_run/ui/server.py", "/server.py", "/static/app.js?x=../../server.py#frag"):
        r = c.request("GET", path)
        if path.startswith("/static/app.js?"):
            assert r.status == 200  # a query on a static file is ignored, never read as a path
            continue
        assert r.status in (400, 404), (path, r.status)
        assert b"import " not in r.body, path


def test_absolute_form_request_target_refused(ui: Harness) -> None:
    c = ui.browser()
    for target in ("http://127.0.0.1:%d/api/snapshot" % ui.port, "*", "api/snapshot"):
        r = c.request("GET", target)
        assert r.status == 400, (target, r.status)
    # The standard library collapses a leading '//' to '/' before the server sees it (against open
    # redirects), so '//127.0.0.1/api/snapshot' arrives as the unknown path '/127.0.0.1/api/snapshot'.
    r = c.request("GET", "//127.0.0.1/api/snapshot")
    assert r.status in (400, 404) and b"seq" not in r.body
