"""
Turn a JSON / JSON Lines file into tables of string rows, so the same
detectors that check spreadsheets can check it. Standard library only.

JSON is a tree, not a table. Every list of records found anywhere in it
becomes a table:

    [{"name": "...", "address": {"city": "..."}}, ...]        one row per record,
                                                              columns "name", "city"
    {"data": {"users": [{...}, {...}]}, "meta": {...}}        the "users" list
    {"uid1": {...}, "uid2": {...}}                            records keyed by id
    [["name", "email"], ["...", "..."]]                       already a table
    {"name": "...", "email": "..."}                           one record: read as
                                                              "field,value" rows

Nested lists of records (each customer's "orders") are gathered into one
table per path. Lists of plain values are joined into one cell.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

Rows = list[list[str]]

MAX_TABLES = 20
MAX_DEPTH = 12
MAX_COLUMNS = 150          # per table: deeply nested trees (lock files) would otherwise explode
FULL_PARSE_LIMIT = 20 * 1024 * 1024        # bytes; bigger files are read partially
_GENERIC_KEYS = {"name", "value", "title", "label", "text"}   # need context: "product" + "name"


class JsonUnreadable(Exception):
    pass


class NotJson(Exception):
    """The file is not JSON at all; read it like any other text file."""


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e16:
        return str(int(v))
    if isinstance(v, (list, tuple)):
        return "; ".join(_cell(x) for x in v if not isinstance(x, (dict, list)))
    return str(v)


def _singular(word: str) -> str:
    word = re.sub(r"[^\w]", "_", word).strip("_")
    if word.lower().endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.lower().endswith("s") and not word.lower().endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


class _Tables:
    """Collects rows per table path, with columns in first-seen order."""

    def __init__(self, max_rows: int):
        self.max_rows = max_rows
        self.tables: dict[str, tuple[str, list[dict[tuple, str]]]] = {}   # path -> (context, rows)
        self.grids: list[Rows] = []                                        # arrays of arrays

    def full(self, path: str) -> bool:
        t = self.tables.get(path)
        return t is not None and len(t[1]) >= self.max_rows

    def add(self, path: str, context: str, record: dict) -> None:
        if path not in self.tables:
            if len(self.tables) + len(self.grids) >= MAX_TABLES:
                return
            self.tables[path] = (context, [])
        rows = self.tables[path][1]
        if len(rows) >= self.max_rows:
            return
        row: dict[tuple, str] = {}
        self._flatten(record, (), row, path, 0)
        rows.append(row)

    def _flatten(self, obj: dict, prefix: tuple, row: dict, path: str, depth: int) -> None:
        for k, v in obj.items():
            if len(row) >= MAX_COLUMNS:
                return
            key = prefix + (str(k),)
            if isinstance(v, dict) and depth < 4:
                self._flatten(v, key, row, path, depth + 1)
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v) and depth < 4:
                child = f"{path}.{'.'.join(key)}"
                for x in v:
                    if self.full(child):
                        break
                    self.add(child, key[-1], x)
            else:
                row[key] = _cell(v)

    def to_rows(self) -> Rows:
        out: Rows = []
        for grid in self.grids:
            out += grid + [[]]
        for context, rows in self.tables.values():
            cols: list[tuple] = list(dict.fromkeys(k for r in rows for k in r))[:MAX_COLUMNS]
            if not cols:
                continue
            out.append(_labels(cols, context))
            out += [[r.get(c, "") for c in cols] for r in rows]
            out.append([])                       # table separator
        return out


def _labels(cols: list[tuple], context: str) -> list[str]:
    """Column labels: the last key ("email"), or the last two when that is
    ambiguous or generic ("billing.city" vs "shipping.city", "product.name")."""
    leaf_count: dict[str, int] = {}
    for c in cols:
        leaf_count[c[-1]] = leaf_count.get(c[-1], 0) + 1
    labels = []
    for c in cols:
        leaf = c[-1]
        label = ".".join(c) if leaf_count[leaf] > 1 else leaf
        if "." not in label and leaf.strip("_").lower() in _GENERIC_KEYS:
            # A bare "name" says nothing in JSON (grammar rules, layouts, packages
            # all have one), so give it its context: "product.name", "customer.name",
            # or "item.name", which leaves the decision to the values.
            # GeoJSON keeps a feature's fields under "properties": use the table name.
            parent = c[-2] if len(c) > 1 and c[-2] != "properties" else _singular(context)
            label = f"{parent or 'item'}.{leaf}"
        labels.append(label)
    return labels


def _walk(node, path: str, context: str, tables: _Tables, depth: int = 0) -> None:
    """Find every list of records (or dict of records keyed by id) in the tree."""
    if depth > MAX_DEPTH or len(tables.tables) + len(tables.grids) >= MAX_TABLES:
        return
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if dicts and len(dicts) >= len(node) / 2:
            for x in dicts:
                if tables.full(path):
                    break
                tables.add(path, context, x)
        elif node and all(isinstance(x, list) for x in node):        # array of arrays: a table
            tables.grids.append([[_cell(v) for v in r] for r in node[: tables.max_rows]])
        elif node and not any(isinstance(x, (dict, list)) for x in node) and len(node) >= 3:
            for v in node[: tables.max_rows]:                           # list of plain values
                tables.add(path, context, {context or "value": v})
        else:
            for x in node[:1000]:
                _walk(x, path, context, tables, depth + 1)
        return
    if not isinstance(node, dict):
        return
    values = list(node.values())
    # {"uid1": {...}, "uid2": {...}}: records keyed by id
    if len(values) >= 3 and all(isinstance(v, dict) for v in values):
        key_sets = [set(v) for v in values[:50]]
        common = set.intersection(*key_sets)
        if common and len(common) >= 0.5 * max(len(k) for k in key_sets):
            for k, v in list(node.items()):
                if tables.full(path):
                    break
                tables.add(path, context, {"key": k, **v})
            return
    for k, v in node.items():
        if isinstance(v, (dict, list)):
            _walk(v, f"{path}.{k}", context if k == "features" and context == "place" else str(k),
                  tables, depth + 1)


def _record_rows(obj: dict) -> Rows:
    """One record as "field,value" rows (nested keys joined with dots)."""
    rows = [["field", "value"]]

    def go(o: dict, prefix: str, depth: int):
        for k, v in o.items():
            if len(rows) > 5000:
                return
            name = f"{prefix}{k}"
            if isinstance(v, dict) and depth < 4:
                go(v, f"{name}.", depth + 1)
            elif not isinstance(v, list) or not any(isinstance(x, (dict, list)) for x in v):
                rows.append([str(k) if not prefix or str(k).lower() not in _GENERIC_KEYS else name, _cell(v)])
    go(obj, "", 0)
    return rows + [[]] if len(rows) > 1 else []


def _strip_comments(text: str) -> str:
    """JSON with comments / trailing commas (VS Code style settings)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _json_lines(text: str, max_rows: int) -> list | None:
    """JSON Lines: one value per line. None unless most lines parse."""
    items, lines = [], [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[: max_rows]:
        try:
            items.append(json.loads(ln))
        except ValueError:
            continue
    return items if lines and len(items) >= 0.8 * min(len(lines), max_rows) else None


def _array_prefix(text: str, max_rows: int) -> list:
    """The first elements of a top-level array, from a file too big to parse whole."""
    dec, items = json.JSONDecoder(), []
    i = text.index("[") + 1
    while len(items) < max_rows:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] == "]":
            break
        try:
            v, i = dec.raw_decode(text, i)
        except ValueError:
            break                                   # cut off by the size limit
        items.append(v)
    return items


def parse(text: str, max_rows: int, truncated: bool = False, lines_hint: bool = False):
    """The JSON value in `text` (a list for JSON Lines)."""
    stripped = text.lstrip("﻿ \t\r\n")
    if lines_hint:
        items = _json_lines(stripped, max_rows)
        if items is not None:
            return items
    if truncated:
        if stripped.startswith("["):
            return _array_prefix(stripped, max_rows)
        items = _json_lines(stripped, max_rows)
        if items is not None:
            return items
        raise JsonUnreadable("too large to inspect quickly (over 20 MB, not a list of records)")
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    items = _json_lines(stripped, max_rows)             # .json that is really JSON Lines
    if items is not None and len(items) > 1:
        return items
    try:
        return json.loads(_strip_comments(stripped))
    except ValueError as e:
        if stripped[:1] in ("{", "["):
            raise JsonUnreadable(f"not valid JSON ({e})")   # broken or cut-off data: ask
        raise NotJson()                                    # a template or text named .json


def _is_schema(data) -> bool:
    """JSON Schema / OpenAPI documents describe fields ("email": {"type":
    "string"}) but hold no values."""
    return isinstance(data, dict) and (
        "$schema" in data or "openapi" in data or "swagger" in data
        or (isinstance(data.get("properties"), dict) and data.get("type") == "object"))


def to_rows(data, max_rows: int, context: str = "") -> Rows:
    if _is_schema(data):
        return []
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        context = "place"                              # GeoJSON map: features are places
    tables = _Tables(max_rows)
    _walk(data, "$", context, tables)
    out = tables.to_rows()
    if not out and isinstance(data, dict):
        out = _record_rows(data)                       # a single record: read as field/value rows
    return out


def read_json(path: Path, max_rows: int, encoding: str) -> Rows:
    size = path.stat().st_size
    with open(path, "r", encoding=encoding, errors="replace") as f:
        text = f.read(FULL_PARSE_LIMIT)
    data = parse(text, max_rows, truncated=size > FULL_PARSE_LIMIT,
                 lines_hint=path.suffix.lower() in (".jsonl", ".ndjson"))
    return to_rows(data, max_rows, context=path.stem)
