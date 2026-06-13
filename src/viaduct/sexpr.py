"""From-scratch s-expression parser/serializer for KiCad files.

KiCad board/schematic files are nested s-expressions with two kinds of
atoms: quoted strings (``"GND"``) and bare tokens (``footprint``, ``50.8``).
The distinction matters on output — KiCad quotes strings and leaves
keywords/numbers bare — so we preserve it:

- quoted string  -> plain ``str``
- bare token     -> ``Sym`` (a ``str`` subclass)

Numbers are kept as their original source text (as ``Sym``) so untouched
values round-trip byte-identically; convert with :func:`to_float` and
create new ones with :func:`num`.
"""

from __future__ import annotations


class Sym(str):
    """A bare (unquoted) s-expression token."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Sym({str.__repr__(self)})"


SExpr = "list | str"  # documentation alias: a node is a list, an atom is str/Sym


class ParseError(ValueError):
    pass


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
_UNESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\\": "\\\\", '"': '\\"'}


def parse(text: str):
    """Parse a complete KiCad file; returns the single top-level list."""
    nodes, pos = _parse_many(text, 0)
    if not nodes:
        raise ParseError("empty document")
    if len(nodes) > 1:
        raise ParseError("multiple top-level expressions")
    return nodes[0]


def _parse_many(text: str, pos: int):
    nodes = []
    n = len(text)
    while True:
        pos = _skip_ws(text, pos)
        if pos >= n or text[pos] == ")":
            return nodes, pos
        node, pos = _parse_one(text, pos)
        nodes.append(node)


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _parse_one(text: str, pos: int):
    c = text[pos]
    if c == "(":
        pos += 1
        children = []
        n = len(text)
        while True:
            pos = _skip_ws(text, pos)
            if pos >= n:
                raise ParseError("unexpected end of input inside list")
            if text[pos] == ")":
                return children, pos + 1
            node, pos = _parse_one(text, pos)
            children.append(node)
    if c == '"':
        return _parse_string(text, pos)
    return _parse_token(text, pos)


def _parse_string(text: str, pos: int):
    pos += 1  # opening quote
    out = []
    n = len(text)
    while pos < n:
        c = text[pos]
        if c == '"':
            return "".join(out), pos + 1
        if c == "\\" and pos + 1 < n:
            nxt = text[pos + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            pos += 2
            continue
        out.append(c)
        pos += 1
    raise ParseError("unterminated string")


def _parse_token(text: str, pos: int):
    start = pos
    n = len(text)
    while pos < n and text[pos] not in ' \t\r\n()"':
        pos += 1
    if pos == start:
        raise ParseError(f"unexpected character {text[pos]!r} at offset {pos}")
    return Sym(text[start:pos]), pos


# ---------------------------------------------------------------------------
# Serialization (KiCad-style pretty printing: tabs, one child list per line)
# ---------------------------------------------------------------------------

def dumps(node) -> str:
    out = []
    _write(node, out, 0)
    out.append("\n")
    return "".join(out)


def _atom_text(a) -> str:
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, str):
        return '"' + "".join(_UNESCAPES.get(c, c) for c in a) + '"'
    raise TypeError(f"not an atom: {a!r}")


def _is_inline(node) -> bool:
    """Lists with no sub-lists print on one line, e.g. ``(size 1 1)``."""
    return isinstance(node, list) and not any(isinstance(c, list) for c in node)


def _write(node, out: list, depth: int) -> None:
    indent = "\t" * depth
    if not isinstance(node, list):
        out.append(indent + _atom_text(node))
        return
    if _is_inline(node):
        out.append(indent + "(" + " ".join(_atom_text(a) for a in node) + ")")
        return
    # head atoms inline, then each child on its own line
    head = []
    i = 0
    while i < len(node) and not isinstance(node[i], list):
        head.append(_atom_text(node[i]))
        i += 1
    out.append(indent + "(" + " ".join(head))
    for child in node[i:]:
        out.append("\n")
        _write(child, out, depth + 1)
    out.append("\n" + indent + ")")


# ---------------------------------------------------------------------------
# Navigation / mutation helpers
# ---------------------------------------------------------------------------

def is_node(x, name: str) -> bool:
    return isinstance(x, list) and len(x) > 0 and isinstance(x[0], Sym) and x[0] == name


def children(node, name: str):
    """All child lists whose head token is *name*."""
    return [c for c in node if is_node(c, name)]


def child(node, name: str):
    """First child list whose head token is *name*, or None."""
    for c in node:
        if is_node(c, name):
            return c
    return None


def atoms(node):
    """The non-list members of a node, minus the head token."""
    return [c for c in node[1:] if not isinstance(c, list)]


def to_float(a) -> float:
    return float(a)


def num(v) -> Sym:
    """Format a number the way KiCad does (no exponent, trimmed zeros)."""
    if isinstance(v, int):
        return Sym(str(v))
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return Sym(s)
