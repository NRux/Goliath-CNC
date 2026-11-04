# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Test doubles and clients for the tests of the local interface server (unit U01; design section 8.2).

Every test talks to a real server on a real loopback socket. The clients here
are written with ``http.client`` and plain sockets, so a test can send exactly
the headers a hostile page (or a confused browser) would send: a foreign
``Host``, no ``Origin``, two cookies, and so on.

* :class:`FakeSource` - a :class:`~goliath_run.types.SnapshotSource` whose
  snapshots the test publishes; it replays from the history it keeps;
* :class:`RecordingSink` - an :class:`~goliath_run.types.IntentSink` that
  records what it was given (or raises, when told to);
* :class:`Client` - one browser: it signs in through the one-time link and
  then holds the session cookie, like a browser does;
* :class:`EventReader` - an ``EventSource``: reads ``text/event-stream``.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from goliath_run.types import Banner, OperatorIntent, Phase, SessionSnapshot

DEFAULT = object()
"""Marker: let the client fill in the header the way a browser on this origin would."""


def make_snapshot(seq: int, **changes: Any) -> SessionSnapshot:
    base = SessionSnapshot(
        seq=seq,
        phase=Phase.IDLE,
        may_be_moving=False,
        banners=(Banner(id="TEST-BANNER", severity="info", text="snapshot %d" % seq, icon="info"),),
        disclosures=(),
        firmware=None,
        firmware_source=None,
        stop_offer="none",
        practice=False,
        progress=None,
    )
    return replace(base, **changes) if changes else base


class FakeSource:
    """A SnapshotSource for tests (the protocol of goliath_run.types).

    ``events(after_seq)`` yields the kept snapshots with ``seq > after_seq``,
    then each new one as the test publishes it, until :meth:`end`. After
    :meth:`forget_through` it starts with the oldest snapshot it still holds,
    as the protocol allows.
    """

    def __init__(self, first_seq: int = 0) -> None:
        self._cond = threading.Condition()
        # (entry number, item, forced): entry numbers only grow; forced items are yielded as they are,
        # whatever their seq (see inject).
        first = make_snapshot(first_seq)
        self._history: list[tuple[int, Any, bool]] = [(0, first, False)]
        self._entries = 1
        self._latest = first
        self._ended = False
        self.events_calls: list[int] = []
        self.fail_events: BaseException | None = None
        self.fail_snapshot: BaseException | None = None

    def _append(self, item: Any, forced: bool) -> None:
        self._history.append((self._entries, item, forced))
        self._entries += 1
        self._cond.notify_all()

    def publish(self, **changes: Any) -> SessionSnapshot:
        with self._cond:
            snap = make_snapshot(self._latest.seq + 1, **changes)
            self._latest = snap
            self._append(snap, False)
            return snap

    def inject(self, item: Any) -> None:
        """Hand ``item`` to the event streams as it is (a source that breaks its contract)."""
        with self._cond:
            self._append(item, True)

    def forget_through(self, seq: int) -> None:
        with self._cond:
            kept = [(n, s, f) for n, s, f in self._history if f or s.seq > seq]
            self._history = kept or [h for h in self._history if h[1] is self._latest]

    def end(self) -> None:
        with self._cond:
            self._ended = True
            self._cond.notify_all()

    def snapshot(self) -> SessionSnapshot:
        if self.fail_snapshot is not None:
            raise self.fail_snapshot
        with self._cond:
            return self._latest

    def events(self, after_seq: int) -> Iterator[SessionSnapshot]:
        with self._cond:
            self.events_calls.append(after_seq)
        if self.fail_events is not None:
            raise self.fail_events
        return self._generate(after_seq)

    def _generate(self, after_seq: int) -> Iterator[SessionSnapshot]:
        last = after_seq
        done = -1  # the last history entry number yielded or passed over
        while True:
            with self._cond:
                while True:
                    pending = [(n, s, f) for n, s, f in self._history if n > done and (f or s.seq > last)]
                    if pending or self._ended:
                        break
                    self._cond.wait()
                if not pending:
                    return
                done, item, forced = pending[0]
            if not forced:
                last = item.seq
            yield item


class RecordingSink:
    """An IntentSink for tests: records every intent; raises ``fail`` when set."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.intents: list[OperatorIntent] = []
        self.fail: BaseException | None = None

    def submit(self, intent: OperatorIntent) -> None:
        if self.fail is not None:
            raise self.fail
        with self._lock:
            self.intents.append(intent)


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def one(self, name: str) -> str:
        values = self.header(name)
        assert len(values) == 1, "expected exactly one %s header, got %r" % (name, values)
        return values[0]

    def json(self) -> Any:
        assert self.one("Content-Type").startswith("application/json"), self.headers
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Client:
    """One browser on this origin (``http://127.0.0.1:<port>``)."""

    port: int
    launch_url: str
    cookie: str | None = None
    lease: str | None = None
    seen: list[Response] = field(default_factory=list)

    @property
    def host(self) -> str:
        return "127.0.0.1:%d" % self.port

    @property
    def origin(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def sign_in(self) -> Response:
        """Open the one-time link and keep the cookie it sets, as a browser does."""
        parts = urlsplit(self.launch_url)
        target = parts.path + ("?" + parts.query if parts.query else "")
        r = self.request("GET", target, cookie=None, fetch_site="none")
        for value in r.header("Set-Cookie"):
            self.cookie = value.split(";", 1)[0].strip()
        return r

    def request(self, method: str, path: str, *, body: bytes | None = None, host: Any = DEFAULT,
                cookie: Any = DEFAULT, origin: Any = DEFAULT, content_type: Any = DEFAULT,
                fetch_site: Any = DEFAULT, headers: list[tuple[str, str]] | None = None) -> Response:
        """One request with explicit headers. ``None`` leaves a header out; a list sends it once per item."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            sent: list[tuple[str, Any]] = [
                ("Host", self.host if host is DEFAULT else host),
                ("Cookie", self.cookie if cookie is DEFAULT else cookie),
            ]
            if method == "POST":
                sent.append(("Origin", self.origin if origin is DEFAULT else origin))
                sent.append(("Content-Type", "application/json" if content_type is DEFAULT else content_type))
                sent.append(("Sec-Fetch-Site", "same-origin" if fetch_site is DEFAULT else fetch_site))
            else:
                if origin is not DEFAULT:
                    sent.append(("Origin", origin))
                if content_type is not DEFAULT:
                    sent.append(("Content-Type", content_type))
                if fetch_site is not DEFAULT:
                    sent.append(("Sec-Fetch-Site", fetch_site))
            for name, value in sent + list(headers or ()):
                for v in value if isinstance(value, list) else [value]:
                    if v is not None:
                        conn.putheader(name, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            r = Response(resp.status, list(resp.getheaders()), resp.read())
        finally:
            conn.close()
        self.seen.append(r)
        return r

    def post_json(self, path: str, data: Any, **kw: Any) -> Response:
        return self.request("POST", path, body=json.dumps(data).encode("utf-8"), **kw)

    def acquire(self, action: str = "acquire") -> Response:
        r = self.post_json("/api/lease", {"action": action, "lease": self.lease})
        if r.status == 200 and "lease" in r.json():
            self.lease = r.json()["lease"]
        return r

    def submit(self, intent: Mapping[str, Any], lease: Any = DEFAULT) -> Response:
        return self.post_json("/api/intent", {"lease": self.lease if lease is DEFAULT else lease,
                                              "intent": dict(intent)})

    def events(self, *, last_event_id: str | None = None,
               extra: list[tuple[str, str]] | None = None) -> EventReader:
        headers = [("Host", self.host), ("Accept", "text/event-stream"), ("Sec-Fetch-Site", "same-origin")]
        if self.cookie is not None:
            headers.append(("Cookie", self.cookie))
        if last_event_id is not None:
            headers.append(("Last-Event-ID", last_event_id))
        return EventReader(self.port, headers + list(extra or ()))


@dataclass
class Event:
    event: str
    data: str
    id: str | None

    def json(self) -> Any:
        return json.loads(self.data)


class EventReader:
    """A minimal EventSource over a plain socket (WHATWG HTML, section Server-sent events)."""

    def __init__(self, port: int, headers: list[tuple[str, str]]) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        lines = ["GET /events HTTP/1.1"] + ["%s: %s" % h for h in headers] + ["", ""]
        self.sock.sendall("\r\n".join(lines).encode("latin-1"))
        self._buf = b""
        head = self._read_until(b"\r\n\r\n", timeout=10)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        self.status = int(status_line.split()[1])
        self.headers = [tuple(x.split(":", 1)) for x in header_lines if x]
        self.headers = [(k.strip(), v.strip()) for k, v in self.headers]
        self.retry: int | None = None
        self.comments: list[str] = []

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no %r within %.1f s; have %r" % (marker, timeout, self._buf[-200:]))
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the server closed the stream; unread: %r" % self._buf)
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def body(self, timeout: float = 5) -> bytes:
        """Everything until the server closes (for a refused stream)."""
        deadline = time.monotonic() + timeout
        while True:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = self.sock.recv(65536)
            if not chunk:
                data, self._buf = self._buf, b""
                return data
            self._buf += chunk

    def next(self, timeout: float = 5, *, skip: tuple[str, ...] = ("heartbeat",)) -> Event:
        """The next dispatched event, skipping ``skip`` types and comments."""
        while True:
            block = self._read_until(b"\n\n", timeout).decode("utf-8")
            event, data, ident = "message", [], None
            for line in block.split("\n"):
                if line.startswith(":"):
                    self.comments.append(line[1:].strip())
                    continue
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "event":
                    event = value
                elif name == "data":
                    data.append(value)
                elif name == "id":
                    ident = value
                elif name == "retry":
                    self.retry = int(value)
            if not data or event in skip:
                continue
            return Event(event, "\n".join(data), ident)

    def snapshots(self, n: int, timeout: float = 5) -> list[Event]:
        out: list[Event] = []
        while len(out) < n:
            e = self.next(timeout, skip=("heartbeat", "lease"))
            assert e.event == "snapshot", e
            out.append(e)
        return out

    def at_eof(self, timeout: float = 5) -> bool:
        try:
            self._read_until(b"\x00never\x00", timeout)
        except EOFError:
            return True
        except TimeoutError:
            return False
        except ConnectionError:  # reset by the server: closed as well
            return True
        return False

    def close(self) -> None:
        self.sock.close()


@dataclass
class Harness:
    server: Any
    source: FakeSource
    sink: RecordingSink

    @property
    def port(self) -> int:
        return self.server.port

    def browser(self, *, signed_in: bool = True, launch_url: str | None = None) -> Client:
        c = Client(self.port, launch_url or self.server.url)
        if signed_in:
            r = c.sign_in()
            assert r.status == 200, (r.status, r.body)
            assert c.cookie, r.headers
        return c

    def second_browser(self) -> Client:
        """Another browser (no cookie), signed in through a newly issued one-time link."""
        return self.browser(launch_url=self.server.new_launch_url())


def parse_stream(data: bytes) -> tuple[list[Event], int | None, list[str]]:
    """Every complete event in ``data`` (an event stream), the last ``retry`` and the comments.

    Follows WHATWG HTML, section Server-sent events: lines end in LF; a blank
    line dispatches; ``data`` lines join with LF; an event without data is not
    dispatched.
    """
    events: list[Event] = []
    retry: int | None = None
    comments: list[str] = []
    blocks = data.decode("utf-8").split("\n\n")
    for block in blocks[:-1]:
        event, lines, ident = "message", [], None
        for line in block.split("\n"):
            if line.startswith(":"):
                comments.append(line[1:].strip())
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                lines.append(value)
            elif name == "id":
                ident = value
            elif name == "retry":
                retry = int(value)
        if lines:
            events.append(Event(event, "\n".join(lines), ident))
    return events, retry, comments


class CollectingWriter:
    """A ``write(bytes)`` for an event stream that keeps everything and can wait for events.

    ``chunks`` keeps each write with the ``time.monotonic()`` it arrived at (the
    stream writes one event per call), so a test can check the gaps between events.
    """

    def __init__(self, *, fail_after: int | None = None, delay_s: float = 0.0) -> None:
        self._cond = threading.Condition()
        self.data = b""
        self.writes = 0
        self.chunks: list[tuple[float, bytes]] = []
        self.fail_after = fail_after
        self.delay_s = delay_s

    def __call__(self, chunk: bytes) -> None:
        if self.delay_s:
            time.sleep(self.delay_s)
        with self._cond:
            if self.fail_after is not None and self.writes >= self.fail_after:
                raise BrokenPipeError(32, "the browser went away")
            self.data += chunk
            self.writes += 1
            self.chunks.append((time.monotonic(), chunk))
            self._cond.notify_all()

    def events(self, *, kinds: tuple[str, ...] | None = None) -> list[Event]:
        with self._cond:
            events = parse_stream(self.data)[0]
        return [e for e in events if kinds is None or e.event in kinds]

    def wait_for(self, n: int, *, kinds: tuple[str, ...] = ("snapshot",), timeout: float = 5) -> list[Event]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [e for e in parse_stream(self.data)[0] if e.event in kinds]
                if len(found) >= n:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("%d %s events after %.1f s, wanted %d; stream: %r"
                                       % (len(found), kinds, timeout, n, self.data[-400:]))
                self._cond.wait(remaining)
