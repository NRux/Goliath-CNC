# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The local interface server (unit U01; the project design, sections 8.1, 8.2 and 8.6).

``serve(source, sink)`` starts a standard-library ``ThreadingHTTPServer`` on
``127.0.0.1`` at a random port and returns a :class:`UIServer`, whose ``url``
is the one-time sign-in link to open in the browser. The browser is a
disposable view: the server reads :class:`~goliath_run.types.SessionSnapshot`
values from a :class:`~goliath_run.types.SnapshotSource` and hands
:data:`~goliath_run.types.OperatorIntent` values to an
:class:`~goliath_run.types.IntentSink`; it never talks to the robot, and HTTP
threads only read snapshots and queue intents.

Endpoints (all need the session cookie except ``/launch``; every response
carries :data:`~goliath_run.ui.security.SECURITY_HEADERS`):

=========================  ==================================================================
``GET /launch?code=...``   one-time sign-in: sets the session cookie, then moves on to ``/``
``GET /``                  the interface (``static/index.html``)
``GET /static/<file>``     the interface's files, read from the package (importlib.resources)
``GET /api/snapshot``      the current snapshot as JSON (``SessionSnapshot.to_json()``)
``GET /events``            Server-Sent Events (:mod:`goliath_run.ui.sse`), ``Last-Event-ID`` replay
``POST /api/lease``        ``{"action": "acquire"|"take"|"release", "lease": <lease or null>}``
``POST /api/intent``       ``{"lease": <lease>, "intent": <OperatorIntent.to_json()>}``; 202 ``{"queued": <type>}``
=========================  ==================================================================

Every request is checked in this order: ``Host`` (DNS rebinding), for a POST
fetch metadata and ``Origin``, the cookie, then the route. An intent is
taken only from the tab that holds the control lease (409 otherwise): the
other tabs are read-only and can take control explicitly. A 202 reply means
the intent is queued for the session, not that the session agrees: the
session decides (it checks the arming phrase, for example) and reports its
answer in a later snapshot (:class:`~goliath_run.types.IntentSink`). A refused request
gets a fixed message and a log line; nothing is refused or dropped silently.
Logs go to the ``goliath_run.ui`` logger and never contain the one-time code
or the session token.

Static files must have a known type (:data:`CONTENT_TYPES`) and a plain name
(:data:`STATIC_NAME`); they are matched by exact name, never by a path the
filesystem interprets, so ``..``, case folding and Windows name aliases
cannot reach anything else.
"""
from __future__ import annotations

import importlib.resources
import json
import logging
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any

from goliath_run.types import IntentSink, SessionSnapshot, SnapshotSource, intent_from_json
from goliath_run.ui.security import (
    LOOPBACK,
    SECURITY_HEADERS,
    ControlLease,
    Credentials,
    Refusal,
    check_fetch_site,
    check_host,
    check_json_content_type,
    check_origin,
    launch_code_from_query,
    redact_request_line,
    socket_options,
)
from goliath_run.ui.sse import HEARTBEAT_S, MAX_STREAMS, EventStream

__all__ = ["CONTENT_TYPES", "MAX_BODY", "STATIC_NAME", "TEXT_SUFFIXES", "UIServer", "serve"]

LOG = logging.getLogger("goliath_run.ui")

STATIC_PACKAGE = "goliath_run.ui"
STATIC_DIR = "static"

STATIC_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
"""A file or folder name in the static tree (a whole path segment): no dot first, no separators."""

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
"""The static file types the server serves; a file of any other type is not served (and a test fails)."""

TEXT_SUFFIXES = frozenset({".html", ".js", ".css", ".svg", ".json", ".txt"})
"""Static types that are text (scanned for external URLs by the tests)."""

MAX_BODY = 64 * 1024
"""The largest POST body read; larger ones get 413."""

MAX_HEARTBEAT_S = HEARTBEAT_S
"""The longest heartbeat interval :func:`serve` takes. The page calls its view stale after three missed
heartbeats of :data:`~goliath_run.ui.sse.HEARTBEAT_S` (``STALE_MS`` in ``static/app.js``), so a longer
interval would make a healthy stream look dead; shorter ones (tests) are fine."""
REQUEST_TIMEOUT_S = 30.0
_DRAIN_LIMIT = 1024 * 1024
_DRAIN_TIMEOUT_S = 1.0
_CLOSE_JOIN_S = 5.0
_CONTENT_LENGTH = re.compile(r"[0-9]{1,10}\Z")
_SECRET_IN_TEXT = re.compile(r"(code=)[^&\s'\"]+")

_LAUNCH_PAGE = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=/">
<title>goliath-run</title>
</head>
<body>
<p>Signed in. <a href="/">Open the goliath-run interface</a>.</p>
</body>
</html>
"""

_GET_ROUTES = ("/", "/api/snapshot", "/events")
_POST_ROUTES = ("/api/intent", "/api/lease")
_FETCH_CHECKED = ("/api/snapshot", "/events")


def _redact(text: str) -> str:
    return _SECRET_IN_TEXT.sub(r"\1<redacted>", text)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("the key %r appears twice" % key)
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ValueError("%s is not a JSON number" % name)


class _App:
    """What the request handlers share: the source, the sink, the credentials, the lease and the open streams."""

    def __init__(self, source: SnapshotSource, sink: IntentSink, port: int, heartbeat_s: float) -> None:
        self.source = source
        self.sink = sink
        self.port = port
        self.heartbeat_s = heartbeat_s
        self.credentials = Credentials(port)
        self.lease = ControlLease()
        self.closing = threading.Event()
        self._lock = threading.Lock()
        self._slots = 0
        self._streams: set[EventStream] = set()
        self.lease.subscribe(lambda _state: self.poke_streams())

    def reserve_stream(self) -> bool:
        with self._lock:
            if self.closing.is_set() or self._slots >= MAX_STREAMS:
                return False
            self._slots += 1
            return True

    def release_stream(self) -> None:
        with self._lock:
            self._slots -= 1

    def register(self, stream: EventStream) -> bool:
        with self._lock:
            if self.closing.is_set():
                return False
            self._streams.add(stream)
            return True

    def unregister(self, stream: EventStream) -> None:
        with self._lock:
            self._streams.discard(stream)

    def poke_streams(self) -> None:
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.poke()

    def close_streams(self) -> None:
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.close()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.0"  # one request per connection; event streams end when the connection does
    timeout = REQUEST_TIMEOUT_S
    error_content_type = "text/plain; charset=utf-8"
    error_message_format = "%(code)d %(message)s\n"

    # -- plumbing ------------------------------------------------------------

    def setup(self) -> None:
        super().setup()
        self._responded = False
        self._body_read = True

    @property
    def _app(self) -> _App:
        app = self.server.app
        if app is None:  # serve() attaches it before the first request is handled
            raise RuntimeError("the interface server has no session attached")
        return app

    def version_string(self) -> str:
        return "goliath-run"

    def send_response(self, code: int, message: str | None = None) -> None:
        self._responded = True
        super().send_response(code, message)

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if isinstance(code, HTTPStatus):
            code = code.value
        LOG.info('%s "%s" %s', self.address_string(), _redact(redact_request_line(self.requestline)), code)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 (the base class's name)
        LOG.warning("%s %s", self.address_string(), _redact(format % args))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the base class's name)
        LOG.info("%s %s", self.address_string(), _redact(format % args))

    def finish(self) -> None:
        super().finish()
        if not self._body_read:
            self._linger()

    def _linger(self) -> None:
        # The browser sent a body this server did not read (a refused POST). Closing a socket with unread
        # input makes the system reset the connection, which can destroy the response before the browser
        # reads it; so first say we are done writing, then read what is left, briefly.
        conn = self.connection
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        try:
            conn.shutdown(socket.SHUT_WR)
            drained = 0
            while drained < _DRAIN_LIMIT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                conn.settimeout(remaining)
                chunk = conn.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError as exc:
            LOG.debug("%s: discarding an unread request body ended with %s", self.address_string(), exc)

    def _respond(self, status: int, body: bytes, content_type: str,
                 headers: Sequence[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any, headers: Sequence[tuple[str, str]] = ()) -> None:
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        self._respond(status, body, "application/json; charset=utf-8", headers)

    def _api(self) -> bool:
        return self.path.startswith("/api/")

    def _error(self, status: int, message: str, headers: Sequence[tuple[str, str]] = (), **extra: Any) -> None:
        if self._api():
            self._json(status, {"error": message, **extra}, headers)
        else:
            self._respond(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8", headers)

    def _refuse(self, refusal: Refusal, headers: Sequence[tuple[str, str]] = ()) -> None:
        LOG.warning('%s "%s" refused (%d): %s', self.address_string(),
                    _redact(redact_request_line(self.requestline)), refusal.status, _redact(refusal.detail))
        self._error(refusal.status, refusal.message, headers)

    def _headers(self, name: str) -> list[str]:
        return self.headers.get_all(name) or []

    # -- methods -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (the base class's naming)
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._body_read = False
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._body_read = False
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._body_read = False
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802 (a CORS preflight is never approved)
        self._dispatch()

    def do_TRACE(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            self._route()
        except Exception as exc:
            LOG.exception('%s "%s" failed', self.address_string(), _redact(redact_request_line(self.requestline)))
            if not self._responded:
                self._error(500, "Internal error in goliath-run: %s: %s. See the goliath-run log."
                            % (type(exc).__name__, exc))

    def _route(self) -> None:
        app = self._app
        refusal = check_host(self._headers("Host"), app.port)
        if refusal is not None:
            return self._refuse(refusal)
        target = self.path
        if not target.startswith("/") or target.startswith("//"):
            return self._refuse(Refusal(400, "Bad request: the request target must be a path.",
                                        "request target %r is not a path" % target[:200]))
        path, _, query = target.partition("?")
        path = path.partition("#")[0]
        method = self.command
        static = path.startswith("/static/")
        if path == "/launch" or path in _GET_ROUTES or static:
            allowed = "GET"
        elif path in _POST_ROUTES:
            allowed = "POST"
        else:
            allowed = None
        if method not in ("GET", "POST") or (allowed is not None and method != allowed):
            return self._error(405, "Method not allowed: %s." % (allowed or "GET or POST"),
                               [("Allow", allowed or "GET, POST")])
        if path == "/launch":
            return self._launch(query)
        if method == "POST":
            refusal = check_fetch_site(self._headers("Sec-Fetch-Site")) or check_origin(
                self._headers("Origin"), self._headers("Host")[0])
            if refusal is not None:
                return self._refuse(refusal)
        refusal = app.credentials.check_cookie(self._headers("Cookie"))
        if refusal is not None:
            return self._refuse(refusal)
        if path in _FETCH_CHECKED:
            refusal = check_fetch_site(self._headers("Sec-Fetch-Site"))
            if refusal is not None:
                return self._refuse(refusal)
        if path == "/":
            return self._static(["index.html"])
        if static:
            return self._static(path[len("/static/"):].split("/"))
        if path == "/api/snapshot":
            return self._snapshot()
        if path == "/events":
            return self._events()
        if path == "/api/intent":
            return self._intent()
        if path == "/api/lease":
            return self._lease()
        return self._error(404, "Not found.")

    # -- sign-in -------------------------------------------------------------

    def _launch(self, query: str) -> None:
        app = self._app
        code = launch_code_from_query(query)
        if code is not None and app.credentials.redeem(code):
            LOG.info("%s signed in with a one-time link", self.address_string())
            return self._respond(200, _LAUNCH_PAGE, "text/html; charset=utf-8",
                                 [("Set-Cookie", app.credentials.set_cookie_header())])
        if app.credentials.check_cookie(self._headers("Cookie")) is None:
            return self._respond(200, _LAUNCH_PAGE, "text/html; charset=utf-8")
        return self._refuse(Refusal(
            403, "This sign-in link has already been used or is not valid: each link works once. If this "
                 "browser is already signed in, open the interface without the link; otherwise ask goliath-run "
                 "for a new link.",
            "one-time code refused (used, unknown or malformed)"))

    # -- static files --------------------------------------------------------

    def _static(self, segments: list[str]) -> None:
        found = _static_file(segments)
        if found is None:
            return self._error(404, "Not found.")
        body, content_type = found
        self._respond(200, body, content_type)

    # -- snapshot and events -------------------------------------------------

    def _snapshot(self) -> None:
        try:
            snap = self._app.source.snapshot()
            if not isinstance(snap, SessionSnapshot):
                raise TypeError("the snapshot source returned %s, not a SessionSnapshot" % type(snap).__name__)
        except Exception as exc:
            LOG.exception("reading the session snapshot failed")
            return self._error(500, "The session snapshot could not be read: %s: %s. See the goliath-run log."
                               % (type(exc).__name__, exc))
        self._json(200, snap.to_json())

    def _client_closed(self) -> bool:
        # An event stream's browser sends nothing after its request, so end-of-input means it closed the
        # connection. A non-blocking peek works the same on Windows and Linux (select() would fail for a
        # descriptor number above FD_SETSIZE in a long-running process). Only this thread uses the socket.
        conn = self.connection
        timeout = conn.gettimeout()
        try:
            conn.settimeout(0.0)
            try:
                return conn.recv(1, socket.MSG_PEEK) == b""
            except (BlockingIOError, InterruptedError):
                return False  # nothing to read: still open
            finally:
                conn.settimeout(timeout)
        except OSError as exc:
            LOG.info("%s: event stream connection unusable (%s)", self.address_string(), exc)
            return True

    def _write_stream(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()

    def _events(self) -> None:
        app = self._app
        if not app.reserve_stream():
            return self._refuse(Refusal(
                503, "Too many open tabs are following this session (%d). Close some tabs and reload." % MAX_STREAMS,
                "event stream limit (%d) reached, or the server is closing" % MAX_STREAMS))
        try:
            try:
                stream = EventStream(app.source, self._write_stream,
                                     last_event_ids=self._headers("Last-Event-ID"),
                                     lease_state=lambda: app.lease.state().to_json(),
                                     heartbeat_s=app.heartbeat_s, client_closed=self._client_closed, logger=LOG)
            except Exception as exc:
                LOG.exception("starting an event stream failed: the session snapshot could not be read")
                return self._error(500, "The session snapshot could not be read: %s: %s. See the goliath-run log."
                                   % (type(exc).__name__, exc))
            if not app.register(stream):
                return self._error(503, "goliath-run is closing the interface.")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                why = stream.run()
                LOG.info("%s event stream ended: %s", self.address_string(), why)
            finally:
                app.unregister(stream)
        finally:
            app.release_stream()

    # -- POST bodies ---------------------------------------------------------

    def _read_json(self) -> tuple[Any, Refusal | None]:
        refusal = check_json_content_type(self._headers("Content-Type"))
        if refusal is not None:
            return None, refusal
        if self._headers("Transfer-Encoding"):
            return None, Refusal(411, "The body needs a Content-Length (no chunked encoding).",
                                 "Transfer-Encoding %r" % self._headers("Transfer-Encoding"))
        lengths = self._headers("Content-Length")
        if not lengths:
            return None, Refusal(411, "The body needs a Content-Length.", "no Content-Length")
        if len(lengths) != 1 or not _CONTENT_LENGTH.match(lengths[0]):
            return None, Refusal(400, "Bad request: Content-Length is not a length.",
                                 "Content-Length %r" % lengths)
        size = int(lengths[0])
        if size > MAX_BODY:
            return None, Refusal(413, "The body is too large (at most %d bytes)." % MAX_BODY,
                                 "Content-Length %d > %d" % (size, MAX_BODY))
        body = self.rfile.read(size)
        self._body_read = True
        if len(body) != size:
            return None, Refusal(400, "Bad request: the body ended early.", "%d of %d bytes" % (len(body), size))
        try:
            text = body.decode("utf-8")
            return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant), None
        except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            return None, Refusal(400, "Bad request: the body is not valid JSON: %s" % exc, "invalid JSON: %s" % exc)

    def _intent(self) -> None:
        app = self._app
        data, refusal = self._read_json()
        if refusal is not None:
            return self._refuse(refusal)
        if not isinstance(data, dict) or set(data) != {"lease", "intent"}:
            return self._refuse(Refusal(400, 'Bad request: the body must be {"lease": ..., "intent": {...}}.',
                                        "intent body with keys %r" % (sorted(data) if isinstance(data, dict)
                                                                      else type(data).__name__)))
        try:
            intent = intent_from_json(data["intent"])
        except (TypeError, ValueError) as exc:
            return self._refuse(Refusal(400, "The intent was refused: %s" % exc, "invalid intent: %s" % exc))
        name = type(intent).__name__
        try:
            held = app.lease.while_holding(data["lease"], lambda: app.sink.submit(intent))
        except Exception as exc:
            LOG.exception("the session did not accept intent %s", name)
            return self._error(500, "The intent could not be queued: %s: %s. See the goliath-run log."
                               % (type(exc).__name__, exc))
        if not held:
            return self._lease_conflict("intent %s from a tab without control" % name)
        LOG.info("%s intent %s queued from the controlling tab", self.address_string(), name)
        # "queued", never "accepted": the session has not answered yet (it answers in a later snapshot).
        self._json(202, {"queued": name})

    def _lease_conflict(self, detail: str) -> None:
        LOG.warning('%s "%s" refused (409): %s', self.address_string(),
                    _redact(redact_request_line(self.requestline)), detail)
        self._json(409, {"error": "This tab is read-only: another tab has control, or none does. Take control to "
                                  "act from this tab.", "state": self._app.lease.state().to_json()})

    def _lease(self) -> None:
        app = self._app
        data, refusal = self._read_json()
        if refusal is not None:
            return self._refuse(refusal)
        usage = 'Bad request: the body must be {"action": "acquire"|"take"|"release", "lease": <lease or null>}.'
        if not isinstance(data, dict) or "action" not in data or not set(data) <= {"action", "lease"}:
            return self._refuse(Refusal(400, usage, "lease body %r" % (sorted(data) if isinstance(data, dict)
                                                                       else type(data).__name__)))
        action, lease = data["action"], data.get("lease")
        if action not in ("acquire", "take", "release") or (lease is not None and type(lease) is not str) \
                or (action == "release" and lease is None):
            return self._refuse(Refusal(400, usage, "lease action %r with a %s lease" % (action,
                                                                                          type(lease).__name__)))
        if action == "acquire":
            grant = app.lease.acquire(lease)
            if grant is None:
                return self._lease_conflict("acquire while another tab holds control")
            return self._json(200, grant.to_json())
        if action == "take":
            grant = app.lease.take()
            LOG.info("%s took control (epoch %d); any other tab is now read-only", self.address_string(), grant.epoch)
            return self._json(200, grant.to_json())
        if not app.lease.release(lease):
            return self._lease_conflict("release by a tab without control")
        LOG.info("%s released control", self.address_string())
        self._json(200, {"released": True, "state": app.lease.state().to_json()})


def _static_file(segments: list[str]) -> tuple[bytes, str] | None:
    """The bytes and type of the static file at ``segments``, matched by exact name; None if there is none."""
    node: Traversable = importlib.resources.files(STATIC_PACKAGE).joinpath(STATIC_DIR)
    for segment in segments:
        if not re.fullmatch(STATIC_NAME, segment) or not node.is_dir():
            return None
        match = next((child for child in node.iterdir() if child.name == segment), None)
        if match is None:
            return None
        node = match
    if not node.is_file():
        return None
    suffix = PurePosixPath(segments[-1]).suffix
    content_type = CONTENT_TYPES.get(suffix)
    if content_type is None:
        LOG.warning("static file %s has no known content type; it is not served", "/".join(segments))
        return None
    return node.read_bytes(), content_type


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # close() ends connections itself and waits a bounded time
    allow_reuse_address = socket_options(os.name).reuse_address
    allow_reuse_port = False
    request_queue_size = 16

    def __init__(self, port: int) -> None:
        self.app: _App | None = None  # set by serve() before the first request is handled
        self._conns: dict[threading.Thread, socket.socket] = {}
        self._conns_lock = threading.Lock()
        super().__init__((LOOPBACK, port), _Handler)

    def server_bind(self) -> None:
        if socket_options(os.name).exclusive_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # Windows only
        # TCPServer's bind, not HTTPServer's: that one looks up the host's name (a DNS query, sometimes slow).
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._conns_lock:
            self._conns[threading.current_thread()] = request
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._conns_lock:
                self._conns.pop(threading.current_thread(), None)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            LOG.info("connection from %s ended: %s", client_address, exc)
        else:
            LOG.exception("error while serving %s", client_address)

    def drop_connections(self) -> list[threading.Thread]:
        """Shut down every open connection (so blocked reads and writes return); the handler threads."""
        with self._conns_lock:
            conns = list(self._conns.items())
        for thread, conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as exc:  # already closed by its handler
                LOG.debug("closing a connection of %s: %s", thread.name, exc)
        return [thread for thread, _ in conns]


def _check_heartbeat(heartbeat_s: object) -> float:
    if isinstance(heartbeat_s, bool) or not isinstance(heartbeat_s, (int, float)):
        raise TypeError("heartbeat_s must be a number of seconds, got %s" % type(heartbeat_s).__name__)
    if not math.isfinite(heartbeat_s) or not 0 < heartbeat_s <= MAX_HEARTBEAT_S:
        raise ValueError("heartbeat_s must be more than 0 and at most %g seconds, got %r"
                         % (MAX_HEARTBEAT_S, heartbeat_s))
    return float(heartbeat_s)


class UIServer:
    """A running interface server (from :func:`serve`).

    * ``url`` - the one-time sign-in link of this launch (open it in the
      browser; it works once);
    * ``origin`` - ``http://127.0.0.1:<port>``; ``port``;
    * ``bound_address`` - the address the listening socket is bound to;
    * :meth:`new_launch_url` - another one-time link (to sign in again, for
      example after the browser was closed);
    * :meth:`close` - stop serving: event streams end, connections close, the
      port is freed. Idempotent. Also a context manager.
    """

    def __init__(self, server: _Server, app: _App, thread: threading.Thread) -> None:
        self._server = server
        self._app = app
        self._thread = thread
        self._closed = False
        self._lock = threading.Lock()
        self.port = server.server_port
        self.origin = "http://%s:%d" % (LOOPBACK, self.port)
        self.url = self._launch_url()

    def __repr__(self) -> str:
        return "UIServer(origin=%r, closed=%r)" % (self.origin, self._closed)  # never the one-time link

    def _launch_url(self) -> str:
        return "%s/launch?code=%s" % (self.origin, self._app.credentials.issue_launch_code())

    @property
    def bound_address(self) -> tuple[str, int]:
        host, port = self._server.socket.getsockname()[:2]
        return str(host), int(port)

    def new_launch_url(self) -> str:
        """Another one-time sign-in link for this launch (the session token stays the same)."""
        with self._lock:
            if self._closed:
                raise RuntimeError("the interface server is closed; start a new one")
            return self._launch_url()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._app.closing.set()
        self._server.shutdown()  # no new connections are accepted after this returns
        self._app.close_streams()
        threads = self._server.drop_connections()
        self._server.server_close()
        self._thread.join(_CLOSE_JOIN_S)
        for thread in threads:
            thread.join(_CLOSE_JOIN_S)
        alive = [t.name for t in threads if t.is_alive()]
        if alive:
            LOG.warning("closing the interface: %d request threads still running after %.0f s: %s",
                        len(alive), _CLOSE_JOIN_S, ", ".join(alive))
        LOG.info("local interface on %s closed", self.origin)

    def __enter__(self) -> UIServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def serve(source: SnapshotSource, sink: IntentSink, *, port: int = 0, heartbeat_s: float = HEARTBEAT_S) -> UIServer:
    """Start the local interface on ``127.0.0.1`` and return it (serving in a background thread).

    ``port`` 0 (the default) lets the system pick a free port. There is no
    way to listen on another address. ``heartbeat_s`` is the event streams'
    heartbeat interval: :data:`goliath_run.ui.sse.HEARTBEAT_S` by default and
    at most (:data:`MAX_HEARTBEAT_S`); tests use shorter ones.
    """
    if not isinstance(source, SnapshotSource):
        raise TypeError("source must be a SnapshotSource (snapshot() and events(after_seq)), got %s"
                        % type(source).__name__)
    if not isinstance(sink, IntentSink):
        raise TypeError("sink must be an IntentSink (submit(intent)), got %s" % type(sink).__name__)
    if type(port) is not int:
        raise TypeError("port must be an int, got %s" % type(port).__name__)
    if not 0 <= port <= 65535:
        raise ValueError("port must be 0 (any free port) or 1-65535, got %d" % port)
    heartbeat = _check_heartbeat(heartbeat_s)
    try:
        server = _Server(port)
    except OSError as exc:
        raise OSError(exc.errno, "cannot listen on %s:%d: %s" % (LOOPBACK, port, exc.strerror or exc)) from exc
    app = _App(source, sink, server.server_port, heartbeat)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              name="goliath-ui-server", daemon=True)
    thread.start()
    ui = UIServer(server, app, thread)
    LOG.info("local interface listening on %s (loopback only; sign in with the one-time link)", ui.origin)
    return ui
