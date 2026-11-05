# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The lexical layer of the strict G-code reader (unit P02; docs/prep/gi-format.md sections 3, 4.1 and 8.1-8.2).

Nothing here decides what a word *means*; it only cuts text into lines, a line
into its code and its comment, the code into words and an annotation comment
into its keyword and items, and says where (line and column) the text breaks
the grammar.

* **Lines** are split by :func:`goliath_link.gcode.read_all_lines`, the same
  .NET ``File.ReadAllLines`` split that goliath-link uses for upload (CRLF, CR
  or LF; a terminator at the very end starts no further line). It is imported,
  never copied, so the reader and the upload path cannot disagree about line
  numbers. One UTF-8 byte-order mark at the very start of the text is removed
  first (:func:`split_lines` says so, and the reader records a note).
* **Columns** are 1-based positions of characters in the line as read.
* **Trailing spaces and tabs** of a line are ignored (gi-format 3.2).
* **Comments** run from the first ``;`` to the end of the line (gi-format 3.3);
  what a comment is (annotation, ``;U``/``;S`` marker, plain comment) is
  decided by :func:`comment_kind`.
* **Words** are an upper-case ASCII letter immediately followed by a number in
  the grammar ``["+" / "-"] 1*DIGIT ["." 1*DIGIT]`` (gi-format 8.1, 8.2). Spaces
  and tabs between words are optional. Anything else is a :class:`LexError`
  naming its column: a lower-case letter, a space between a letter and its
  number, ``.5``, ``5.``, an exponent, a comma, a ``(`` or ``%``, a non-ASCII
  character. Numbers are kept exactly as written (:class:`decimal.Decimal` built
  from the text, never binary floating point).
* **Annotations** (``;GOLIATH ...``) follow the grammar of gi-format 4.1;
  :func:`lex_annotation` returns the keyword and the items, or the column and
  reason of the first error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from goliath_link.gcode import read_all_lines

__all__ = [
    "BOM",
    "Annotation",
    "AnnotationError",
    "AnnotationItem",
    "CommentKind",
    "LexError",
    "LexedLine",
    "Word",
    "comment_kind",
    "is_annotation_like",
    "lex_annotation",
    "lex_line",
    "split_lines",
    "version_line_token",
]

BOM = "\ufeff"
"""The byte-order mark a reader ignores once at the very start of a file (gi-format 3.1)."""

_WSP = " \t"
_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_ANNOTATION_KEYWORD = re.compile(r"[A-Z][A-Z0-9]*")
_KEY_EQ = re.compile(r"([a-z][a-z0-9_]*)=")
_TOKEN = re.compile(r"[A-Za-z0-9_.+-]+")

CommentKind = Literal["annotation", "marker", "plain"]


def split_lines(text: str) -> tuple[list[str], bool]:
    """The lines of ``text`` as goliath-link splits them, and whether a leading BOM was removed.

    Only one BOM, and only at the very start, is removed; a second one is the
    first character of line 1 and is reported by whoever reads that line.
    """
    if not isinstance(text, str):
        raise TypeError("the G-code text must be a str (decode the file as UTF-8 first), got %s"
                        % type(text).__name__)
    had_bom = text.startswith(BOM)
    if had_bom:
        text = text[1:]
    return read_all_lines(text), had_bom


@dataclass(frozen=True)
class Word:
    """One G-code word: ``letter`` and the number ``text`` as written, its exact ``value`` and its column."""

    letter: str
    text: str
    value: Decimal
    column: int

    @property
    def integral(self) -> bool:
        """True when the number has no decimal point."""
        return "." not in self.text

    @property
    def signed(self) -> bool:
        """True when the number carries a ``+`` or ``-``."""
        return self.text[:1] in ("+", "-")

    @property
    def digits_only(self) -> bool:
        """True when the number is digits only: no sign, no point (``G``, ``M``, ``T``, ``H``, ``N``)."""
        return self.integral and not self.signed

    def __str__(self) -> str:
        return self.letter + self.text


@dataclass(frozen=True)
class LexError:
    """The first place a line breaks the lexical grammar: its 1-based column and why."""

    column: int
    reason: str


@dataclass(frozen=True)
class LexedLine:
    """A line cut into its code and comment (trailing spaces and tabs ignored) and the words of its code.

    ``raw`` is the line exactly as read (the text of its source reference).
    ``comment`` is the text after the first ``;`` (``None`` when there is no
    ``;``), ``comment_column`` the column of that ``;``. ``words`` holds the
    words before ``error``; when ``error`` is set the line is refused and its
    words must not be interpreted.
    """

    number: int
    raw: str
    code: str
    comment: str | None
    comment_column: int | None
    words: tuple[Word, ...]
    error: LexError | None

    @property
    def has_code(self) -> bool:
        """True when the part before any ``;`` holds anything but spaces and tabs."""
        return bool(self.code.strip(_WSP))

    @property
    def blank(self) -> bool:
        """True for a line with neither code nor comment (spaces and tabs only, or empty)."""
        return not self.has_code and self.comment is None


def _describe(ch: str) -> str:
    if ch.isascii() and ch.isprintable():
        return repr(ch)
    return "U+%04X" % ord(ch)


def lex_line(number: int, raw: str) -> LexedLine:
    """Cut ``raw`` (line ``number``) into code, comment and words (gi-format 3.2, 3.3, 8.1, 8.2)."""
    line = raw.rstrip(_WSP)
    semi = line.find(";")
    if semi < 0:
        code, comment, comment_column = line, None, None
    else:
        code, comment, comment_column = line[:semi], line[semi + 1:], semi + 1
    words, error = _lex_code(code)
    return LexedLine(number, raw, code, comment, comment_column, words, error)


def _lex_code(code: str) -> tuple[tuple[Word, ...], LexError | None]:
    words: list[Word] = []
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        column = i + 1
        if ch in _WSP:
            i += 1
            continue
        if not ch.isascii():
            return tuple(words), LexError(column, "non-ASCII character %s outside a comment (the robot receives "
                                                  "ASCII only; a typographic dash pasted for a minus sign is a "
                                                  "typical cause)" % _describe(ch))
        if ch == "(":
            return tuple(words), LexError(column, "a parenthesised comment: comments start with ';'")
        if ch == "%":
            return tuple(words), LexError(column, "a '%' program sentinel is not part of the accepted G-code")
        if "a" <= ch <= "z":
            return tuple(words), LexError(column, "lower-case letter %r: word letters are upper case" % ch)
        if not ("A" <= ch <= "Z"):
            if ch in "+-." or "0" <= ch <= "9":
                return tuple(words), LexError(column, "a number without a word letter before it")
            return tuple(words), LexError(column, "unexpected character %s" % _describe(ch))
        match = _NUMBER.match(code, i + 1)
        if match is None:
            return tuple(words), LexError(column + 1, _no_number_reason(ch, code, i + 1))
        end = match.end()
        if end < n:
            follower = code[end]
            if follower not in _WSP and not ("A" <= follower <= "Z"):
                return tuple(words), LexError(end + 1, _after_number_reason(ch + match.group(), follower))
        words.append(Word(ch, match.group(), Decimal(match.group()), column))
        i = end
    return tuple(words), None


def _no_number_reason(letter: str, code: str, at: int) -> str:
    if at >= len(code):
        return "letter %s without a number" % letter
    nxt = code[at]
    if nxt in _WSP:
        return "a space between the letter %s and its number: write them together" % letter
    if nxt == ".":
        return "a number needs a digit before its point (write 0.5, not .5)"
    if nxt in "+-":
        rest = code[at + 1:at + 2]
        if rest == ".":
            return "a number needs a digit before its point (write -0.5, not -.5)"
        return "a sign must be followed by a digit"
    return "letter %s must be followed by a number, not %s" % (letter, _describe(nxt))


def _after_number_reason(word: str, follower: str) -> str:
    if follower == ".":
        return "after %s: a point must be followed by a digit, and a number has one point at most" % word
    if follower == ",":
        return "after %s: the decimal separator is a point, not a comma" % word
    if follower in "eE":
        return "after %s: numbers have no exponent" % word
    if "a" <= follower <= "z":
        return "after %s: lower-case letter %r (word letters are upper case; numbers have no exponent)" % (
            word, follower)
    if not follower.isascii():
        return "after %s: non-ASCII character %s outside a comment" % (word, _describe(follower))
    if follower == "(":
        return "after %s: a parenthesised comment: comments start with ';'" % word
    return "after %s: unexpected character %s" % (word, _describe(follower))


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def is_annotation_like(comment: str | None) -> bool:
    """True when a comment's text, ignoring leading spaces and tabs, starts with GOLIATH in any letter case.

    Such a comment must be a well-formed annotation line (gi-format 3.3, 4.1):
    a mistyped annotation is refused, never dropped as a plain comment.
    """
    return comment is not None and comment.lstrip(_WSP).upper().startswith("GOLIATH")


def comment_kind(comment: str) -> CommentKind:
    """What a comment is (gi-format 3.3): an annotation, a ``;U``/``;S`` marker, or a plain comment.

    A marker is any comment whose first character that is not a space or a tab
    is ``U``, ``u``, ``S`` or ``s`` (gi-format 3.4: recognition is deliberately
    broad).
    """
    if is_annotation_like(comment):
        return "annotation"
    stripped = comment.lstrip(_WSP)
    if stripped[:1] in ("U", "u", "S", "s"):
        return "marker"
    return "plain"


# ---------------------------------------------------------------------------
# Annotations (gi-format 4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnnotationItem:
    """One item of an annotation: ``key`` (None for a positional value), the value unescaped, how it was written."""

    key: str | None
    value: str
    quoted: bool
    column: int


@dataclass(frozen=True)
class Annotation:
    """A well-formed ``;GOLIATH <KEYWORD> <items>`` line."""

    keyword: str
    keyword_column: int
    items: tuple[AnnotationItem, ...]


@dataclass(frozen=True)
class AnnotationError:
    """Why an annotation-like line is not a well-formed annotation (``D-GI-SYNTAX``), and where."""

    column: int
    reason: str


def lex_annotation(line: LexedLine) -> Annotation | AnnotationError:
    """Read a comment-only line that starts with ``;GOLIATH`` by the grammar of gi-format 4.1.

    The version line ``;GOLIATH-GI <n>`` is not an annotation here: callers
    recognise it first (``version_line_token``).
    """
    if line.has_code or line.comment_column != 1:
        return AnnotationError(line.comment_column or 1, "an annotation is a line of its own: ';GOLIATH' must "
                                                         "start in column 1, with no G-code before it")
    text = line.raw.rstrip(_WSP)
    if not text.startswith(";GOLIATH"):
        return AnnotationError(2, "an annotation starts with ';GOLIATH' in upper case, with no space between "
                                  "';' and 'GOLIATH'")
    pos = len(";GOLIATH")
    n = len(text)
    if pos >= n or text[pos] not in _WSP:
        return AnnotationError(pos + 1, "';GOLIATH' must be followed by a space or a tab and a keyword")
    while pos < n and text[pos] in _WSP:
        pos += 1
    match = _ANNOTATION_KEYWORD.match(text, pos)
    if match is None:
        return AnnotationError(pos + 1, "an annotation keyword is upper-case letters and digits, starting with a "
                                        "letter")
    keyword = match.group()
    keyword_column = pos + 1
    pos = match.end()
    items: list[AnnotationItem] = []
    keys: set[str] = set()
    while pos < n:
        if text[pos] not in _WSP:
            return AnnotationError(pos + 1, "unexpected character %s: items are separated by spaces or tabs, "
                                            "keys are lower case and written key=value" % _describe(text[pos]))
        while pos < n and text[pos] in _WSP:
            pos += 1
        if pos >= n:
            break
        start = pos
        key = None
        key_match = _KEY_EQ.match(text, pos)
        if key_match is not None:
            key = key_match.group(1)
            pos = key_match.end()
            if key in keys:
                return AnnotationError(start + 1, "key %r given twice on one line" % key)
            keys.add(key)
        if pos < n and text[pos] == '"':
            parsed = _quoted(text, pos)
            if isinstance(parsed, AnnotationError):
                return parsed
            value, pos = parsed
            items.append(AnnotationItem(key, value, True, start + 1))
            continue
        token = _TOKEN.match(text, pos)
        if token is None:
            what = "after %s=" % key if key is not None else "here"
            return AnnotationError(pos + 1, "a value is expected %s: a token of letters, digits, '_', '.', '+', "
                                            "'-', or a quoted text" % what)
        items.append(AnnotationItem(key, token.group(), False, start + 1))
        pos = token.end()
    return Annotation(keyword, keyword_column, tuple(items))


def _quoted(text: str, pos: int) -> tuple[str, int] | AnnotationError:
    """The unescaped quoted value starting at ``text[pos] == '"'`` and the position after it."""
    out: list[str] = []
    i = pos + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt not in ('"', "\\"):
                return AnnotationError(i + 1, "in a quoted value a backslash may only escape '\"' or '\\'")
            out.append(nxt)
            i += 2
            continue
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return AnnotationError(i + 1, "control character %s inside a quoted value" % _describe(ch))
        out.append(ch)
        i += 1
    return AnnotationError(pos + 1, "quoted value without its closing quote")


def version_line_token(line: LexedLine) -> str | None:
    """For a line that starts ``;GOLIATH-GI`` and a space or tab: everything after them (trailing spaces ignored).

    None for any other line. The caller decides whether the token is a valid
    version (gi-format 5: a whole number without leading zeros).
    """
    text = line.raw.rstrip(_WSP)
    prefix = ";GOLIATH-GI"
    if not text.startswith(prefix) or len(text) == len(prefix) or text[len(prefix)] not in _WSP:
        return None
    return text[len(prefix):].lstrip(_WSP)

