# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Static (AST) helpers for the architecture tests.

Architecture tests state facts about the code itself: "``upload_gcode`` is called
from exactly one place", "no never-send command literal appears in
``goliath_run``". These helpers answer such questions by parsing the source,
without importing or running it:

* :func:`find_calls` - where is a function or method called?
* :func:`find_references` - the same, plus bare references
  (``send = client.upload_gcode``), assignments to the name and imports of it;
* :func:`find_string_literals` - where does a string or bytes literal match a
  regular expression?

Each returns a sorted list of :class:`Location`.

**Matching is conservative on purpose.** A guard that misses a call passes
silently, so when static analysis cannot decide, the candidate is reported and
marked rather than left out:

* A call or reference matches when its name - the last component, after
  imports are resolved - equals the last component of ``dotted_name``.
* ``Location.exact`` is True when the name as written, or as resolved through
  the module's imports and top-level definitions, equals ``dotted_name`` or ends
  with it at a dot: after ``import subprocess as sp``, ``sp.run(...)`` is
  exactly ``subprocess.run``. It is False when the receiver is a value known
  only at run time (``self._client.upload_gcode``, ``make()(...)``), a name from
  a star import, or an import path that differs from the query (a re-export
  such as ``goliath_link.GoliathClient`` against
  ``goliath_link.client.GoliathClient``, or another module's function with the
  same name). Filtering on ``exact`` is a choice the test makes visibly.
* Querying a bare name (``"upload_gcode"``) finds every call of it, whatever the
  receiver, and every such match is exact.

**What these helpers cannot see**, so a guard must not rely on them for it:

* dynamic access: ``getattr(obj, "upload_gcode")`` (pair the guard with
  :func:`find_string_literals`), ``exec``/``eval``, ``importlib.import_module``;
* strings built at run time beyond the folds below: ``" ".join(...)``, ``%`` and
  ``.format`` formatting, f-string placeholders (shown as ``{...}``);
* comments, and files that are not ``.py`` (JSON, HTML, JavaScript).

Literal kinds: ``string``, ``docstring`` (the first statement of a module,
class or function), ``bytes`` (matched as latin-1 text), ``fstring`` (literal
parts joined, each placeholder as ``{...}``) and ``concat`` (a chain of ``+``
over literals of one type, folded and reported once, at its start). Adjacent
literals (``"A" "B"``) are joined by the parser and reported as one. The
literal parts of a template string (``t"..."``, Python 3.14) are reported one
by one as ``string``.

A guard that asserts *absence* should also show that its query finds a known
positive example (a fixture, or the one allowed call site), so a typo in the
query cannot make it pass.

**Errors are never an empty answer.** A package that cannot be found, a folder
without Python files and a file that does not parse raise
:class:`AstScanError`; a malformed ``dotted_name`` raises ``ValueError`` and a
malformed pattern ``re.error``.

``package`` is an importable dotted name (``"goliath_run"``), located with
:func:`importlib.util.find_spec` (a top-level package is located, not
imported; the parents of a sub-package are imported), or a path to a package
folder or to one ``.py`` file. A string made only of identifiers and dots is
taken as a name; to scan a folder with such a name, pass a path that contains
a separator (``./name``). Every ``.py`` file below the folder is read.

Python 3.9 compatible: the repository-level suite runs on 3.9. Files are parsed
by the running interpreter, so code that needs a newer grammar must be scanned
from a test that runs on that newer Python.
"""
from __future__ import annotations

import ast
import importlib.util
import platform
import re
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple, Union

__all__ = [
    "AstScanError",
    "Location",
    "find_calls",
    "find_references",
    "find_string_literals",
    "package_files",
]

#: Kinds of a Location from find_references (find_calls returns only "call").
REFERENCE_KINDS = ("call", "reference", "store", "import")
#: Kinds of a Location from find_string_literals.
LITERAL_KINDS = ("string", "docstring", "bytes", "fstring", "concat")

_UNKNOWN_ROOT = "<expr>"
_PLACEHOLDER = "{...}"


class AstScanError(Exception):
    """A package or file could not be scanned. Raised instead of an empty answer."""


class Location(NamedTuple):
    """One match.

    ``path`` is the absolute path of the file, ``module`` its dotted module name,
    ``line`` 1-based and ``col`` 0-based (as in :mod:`ast`; both point at the
    start of the expression). ``kind`` is one of REFERENCE_KINDS or
    LITERAL_KINDS. ``text`` is the dotted name (resolved through imports when
    it could be) or the literal's value. ``exact`` is explained in the module
    docstring; it is always True for literals.
    """

    path: str
    module: str
    line: int
    col: int
    kind: str
    text: str
    exact: bool = True

    def __str__(self) -> str:
        note = "" if self.exact else " (not exact)"
        return "%s:%d:%d: %s %r%s" % (self.path, self.line, self.col + 1, self.kind, self.text, note)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def find_calls(package: str, dotted_name: str) -> List[Location]:
    """Every call in `package` whose called name matches `dotted_name` (see the module docstring)."""
    return [loc for loc in find_references(package, dotted_name) if loc.kind == "call"]


def find_references(package: str, dotted_name: str) -> List[Location]:
    """Every call, bare reference, assignment and import in `package` matching `dotted_name`."""
    query = _query_parts(dotted_name)
    out: List[Location] = []
    for module in _modules(package):
        out.extend(module.references(query))
    return sorted(out)


def find_string_literals(package: str, pattern: str) -> List[Location]:
    """Every literal in `package` for which ``re.search(pattern, text)`` matches."""
    if not isinstance(pattern, str):
        raise TypeError("pattern must be a str (a regular expression), got %r" % (pattern,))
    regex = re.compile(pattern)
    out: List[Location] = []
    for module in _modules(package):
        out.extend(module.literals(regex))
    return sorted(out)


def package_files(package: str) -> List[Tuple[Path, str]]:
    """(path, dotted module name) of every Python file `package` resolves to, sorted by path."""
    return [(source.path, source.module) for source in _sources(package)]


# --------------------------------------------------------------------------
# Locating the files
# --------------------------------------------------------------------------


class _Source(NamedTuple):
    path: Path
    module: str
    is_package: bool  # an __init__.py: relative imports resolve against the module itself


def _is_dotted_name(text: str) -> bool:
    return all(part.isidentifier() for part in text.split("."))


def _query_parts(dotted_name: str) -> List[str]:
    if not isinstance(dotted_name, str):
        raise TypeError("dotted_name must be a str, got %r" % (dotted_name,))
    if not dotted_name or not _is_dotted_name(dotted_name):
        raise ValueError("dotted_name must be identifiers joined by dots, got %r" % (dotted_name,))
    return dotted_name.split(".")


def _package_name_of_dir(folder: Path) -> str:
    """Dotted name of `folder` if it is a package (climbing parents with __init__.py), else ''."""
    names: List[str] = []
    current = folder
    while (current / "__init__.py").is_file() and current.name.isidentifier():
        names.insert(0, current.name)
        if current.parent == current:
            break
        current = current.parent
    return ".".join(names)


def _module_name(base: str, rel_parts: Tuple[str, ...]) -> Tuple[str, bool]:
    parts = [base] if base else []
    parts.extend(rel_parts)
    is_package = bool(parts) and parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _roots_from_name(package: str) -> List[Tuple[Path, str]]:
    try:
        spec = importlib.util.find_spec(package)
    except Exception as exc:  # a parent package failed to import: say so, never answer empty
        raise AstScanError("cannot locate package %r: %s: %s" % (package, type(exc).__name__, exc)) from exc
    if spec is None:
        raise AstScanError(
            "package %r is not importable from this interpreter (not installed, or not on sys.path); "
            "to scan a folder or file by path, pass a path with a separator, such as ./%s" % (package, package)
        )
    locations = list(spec.submodule_search_locations or [])
    if locations:
        return [(Path(location), package) for location in locations]
    origin = spec.origin or ""
    if origin.endswith(".py"):
        return [(Path(origin), package)]
    raise AstScanError("%r has no Python source to read (origin: %r)" % (package, spec.origin))


def _sources(package: str) -> List[_Source]:
    if not isinstance(package, str) or not package:
        raise AstScanError("package must be a non-empty dotted name or path, got %r" % (package,))
    if _is_dotted_name(package):
        roots = _roots_from_name(package)
    else:
        path = Path(package)
        if not path.exists():
            raise AstScanError("%s does not exist" % path)
        path = path.resolve()
        if path.is_file():
            if path.suffix != ".py":
                raise AstScanError("%s is not a .py file" % path)
            roots = [(path, "")]
        else:
            roots = [(path, _package_name_of_dir(path))]
    found: Dict[Path, _Source] = {}
    for root, base in roots:
        root = root.resolve()
        if root.is_file():
            if base:  # located by name: the module's own name
                found[root] = _Source(root, base, root.name == "__init__.py")
            else:  # a file given by path: name it from the packages around it
                pkg = _package_name_of_dir(root.parent)
                name, is_package = _module_name(pkg, (root.stem,))
                found[root] = _Source(root, name, is_package)
            continue
        if not root.is_dir():
            raise AstScanError("%s is neither a file nor a folder" % root)
        for path in root.rglob("*.py"):
            if not path.is_file():
                continue
            name, is_package = _module_name(base, path.relative_to(root).with_suffix("").parts)
            found[path.resolve()] = _Source(path.resolve(), name, is_package)
    if not found:
        raise AstScanError("no Python files under %s" % ", ".join(str(r) for r, _ in roots))
    return [found[p] for p in sorted(found)]


def _parse(path: Path) -> ast.Module:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AstScanError("cannot read %s: %s" % (path, exc)) from exc
    try:
        return ast.parse(data, filename=str(path))
    except (SyntaxError, ValueError) as exc:  # ValueError: e.g. a NUL byte, on older Pythons
        raise AstScanError("cannot parse %s with Python %s: %s" % (path, platform.python_version(), exc)) from exc


def _modules(package: str) -> Iterator["_Module"]:
    for source in _sources(package):
        yield _Module(source, _parse(source.path))


# --------------------------------------------------------------------------
# One parsed module
# --------------------------------------------------------------------------

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp,
           ast.DictComp, ast.GeneratorExp)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class _Module:
    def __init__(self, source: _Source, tree: ast.Module) -> None:
        self.source = source
        self.tree = tree
        self.parents: Dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        # name -> qualified names an import binds it to (several when imports disagree)
        self.imports: Dict[str, List[str]] = {}
        # (import node, qualified name it imports)
        self.imported: List[Tuple[ast.AST, str]] = []
        # names bound at module level by a definition or an assignment
        self.toplevel: Set[str] = set()
        self._collect_bindings()

    # -- bindings ----------------------------------------------------------

    def _at_module_level(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, ast.Module):
            if isinstance(current, _SCOPES):
                return False
            current = self.parents.get(current)
        return True

    def _bind_import(self, name: str, qualified: str) -> None:
        bound = self.imports.setdefault(name, [])
        if qualified not in bound:
            bound.append(qualified)

    def _from_base(self, node: ast.ImportFrom) -> Optional[str]:
        """The absolute module a ``from ... import`` reads from, or None if it cannot be resolved."""
        if not node.level:
            return node.module or None
        parts = self.source.module.split(".") if self.source.module else []
        if not self.source.is_package:
            parts = parts[:-1]
        drop = node.level - 1
        if drop > len(parts) or not parts[: len(parts) - drop]:
            return None
        parts = parts[: len(parts) - drop]
        if node.module:
            parts.append(node.module)
        return ".".join(parts)

    def _collect_bindings(self) -> None:
        # Bindings are collected over the whole module, whatever the scope: a
        # name a function shadows still gets the import's resolution too, which
        # can only add matches, never hide one.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self._bind_import(alias.asname, alias.name)
                    else:
                        top = alias.name.split(".")[0]
                        self._bind_import(top, top)
                    self.imported.append((node, alias.name))
            elif isinstance(node, ast.ImportFrom):
                base = self._from_base(node)
                if base is None:
                    continue  # a relative import above the top package: its names stay as written
                for alias in node.names:
                    if alias.name == "*":
                        continue  # names from a star import stay as written: matched by name, not exact
                    qualified = "%s.%s" % (base, alias.name)
                    self._bind_import(alias.asname or alias.name, qualified)
                    self.imported.append((node, qualified))
            elif isinstance(node, _DEFINITIONS):
                if self._at_module_level(node):
                    self.toplevel.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                if self._at_module_level(node):
                    self.toplevel.add(node.id)

    # -- names -------------------------------------------------------------

    def _candidates(self, node: ast.AST) -> Tuple[List[List[str]], List[List[str]]]:
        """(resolved, written) dotted forms of a Name or Attribute chain.

        The written form is always a candidate, so a match never depends on
        resolution succeeding; resolution adds forms (and can make a match exact).
        """
        attrs: List[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            attrs.append(current.attr)
            current = current.value
        attrs.reverse()
        if not isinstance(current, ast.Name):
            return [], [[_UNKNOWN_ROOT] + attrs]
        root = current.id
        written = [root] + attrs
        resolved: List[List[str]] = []
        for qualified in self.imports.get(root, []):
            resolved.append(qualified.split(".") + attrs)
        if root in self.toplevel and self.source.module:
            resolved.append(self.source.module.split(".") + written)
        return resolved, [written]

    def _location(self, node: ast.AST, kind: str, text: str, exact: bool) -> Location:
        return Location(
            str(self.source.path), self.source.module, node.lineno, node.col_offset, kind, text, exact
        )

    def references(self, query: List[str]) -> Iterator[Location]:
        last = query[-1]
        n = len(query)
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            resolved, written = self._candidates(node)
            forms = resolved + written
            if not any(form[-1] == last for form in forms):
                continue
            exact = any(form[-n:] == query for form in forms)
            best = next((f for f in resolved if f[-n:] == query), None) or (resolved[0] if resolved else written[0])
            parent = self.parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                kind = "call"
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                kind = "store"
            else:
                kind = "reference"
            yield self._location(node, kind, ".".join(best), exact)
        for node, qualified in self.imported:
            parts = qualified.split(".")
            if parts[-1] == last:
                yield self._location(node, "import", qualified, parts[-n:] == query)

    # -- literals ----------------------------------------------------------

    def _docstrings(self) -> Set[ast.AST]:
        found: Set[ast.AST] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Module,) + _DEFINITIONS) and node.body:
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    found.add(first.value)
        return found

    def _is_add(self, node: Optional[ast.AST]) -> bool:
        return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)

    def literals(self, regex: "re.Pattern[str]") -> Iterator[Location]:
        docstrings = self._docstrings()
        for node in ast.walk(self.tree):
            parent = self.parents.get(node)
            if isinstance(node, ast.Constant):
                if isinstance(parent, ast.JoinedStr):
                    continue  # part of an f-string: reported with the f-string
                if isinstance(node.value, str):
                    text, kind = node.value, ("docstring" if node in docstrings else "string")
                elif isinstance(node.value, bytes):
                    text, kind = node.value.decode("latin-1"), "bytes"
                else:
                    continue
            elif isinstance(node, ast.JoinedStr):
                if isinstance(parent, ast.FormattedValue):
                    continue  # a format spec inside an f-string
                text, kind = _fstring_text(node), "fstring"
            elif self._is_add(node):
                if self._is_add(parent) and _fold(parent) is not None:
                    continue  # an inner link of a longer chain: reported once, at the whole chain
                folded = _fold(node)
                if folded is None:
                    continue
                text = folded.decode("latin-1") if isinstance(folded, bytes) else folded
                kind = "concat"
            else:
                continue
            if regex.search(text):
                yield self._location(node, kind, text, True)


def _fstring_text(node: ast.JoinedStr) -> str:
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append(_PLACEHOLDER)
    return "".join(parts)


def _fold(node: ast.AST) -> Union[str, bytes, None]:
    """The value of a ``+`` chain over literals of one type, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None or type(left) is not type(right):
            return None
        return left + right  # type: ignore[operator]
    return None
