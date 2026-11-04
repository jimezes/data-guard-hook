"""
Read the first N rows of a tabular file into a list of string rows.
Standard library only, so there's nothing to pip install.

To support a new format, add a function `read_<ext>(path, max_rows)` and
register it in READERS.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import jsondata
import xls

Rows = list[list[str]]


class UnreadableFile(Exception):
    """The file has a guarded extension but we can't inspect it."""


# Tried, in order, when a file is not valid UTF-8 (older Excel/Windows exports).
_LEGACY_ENCODINGS = ("cp1253", "iso-8859-7", "cp1252")


def _is_greek(ch: str) -> bool:
    return "Ͱ" <= ch <= "Ͽ"


def _legacy_encoding(raw: bytes) -> str:
    """Pick the single-byte code page that fits the bytes best.

    cp1253/ISO-8859-7 (Greek) and cp1252 (Western) decode almost any bytes,
    so "no errors" proves little. Read as Greek, real Greek gives whole Greek
    words, while Western text gives Latin words with stray Greek letters
    in them. Greek wins when its pure-Greek words
    outnumber those mixed ones; between the two Greek code pages, the one
    yielding more Greek letters wins (they differ mainly in where "Ά" sits)."""
    best, best_score = "cp1252", 0
    for enc in _LEGACY_ENCODINGS[:2]:
        text = raw.decode(enc, errors="replace")
        pure = mixed = 0
        for word in re.findall(r"[^\W\d_]+", text):
            if any(map(_is_greek, word)):
                if all(map(_is_greek, word)):
                    pure += 1
                else:
                    mixed += 1
        if pure > mixed:
            score = sum(map(_is_greek, text))
            if score > best_score:
                best, best_score = enc, score
    return best


def _detect_encoding(path: Path) -> str:
    """UTF-8 unless a BOM or the NUL-byte pattern says UTF-16/32, or the bytes
    aren't valid UTF-8 (then a legacy Greek/Western code page)."""
    with open(path, "rb") as f:
        raw = f.read(64 * 1024)
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw and raw.count(b"\x00") > len(raw) // 4:          # UTF-16 without a BOM
        even, odd = raw[0::2].count(b"\x00"), raw[1::2].count(b"\x00")
        return "utf-16-be" if even > odd else "utf-16-le"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte character cut off by the 64 KB read limit is still UTF-8.
        if not (e.reason == "unexpected end of data" and e.start >= len(raw) - 3):
            return _legacy_encoding(raw)
    return "utf-8-sig"


def read_delimited(path: Path, max_rows: int, delimiter: str | None = None) -> Rows:
    encoding = _detect_encoding(path)
    with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
        head = f.read(64 * 1024)
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        f.seek(0)
        rows: Rows = []
        for row in csv.reader(f, delimiter=delimiter):
            rows.append(row)
            if len(rows) >= max_rows:
                break
    # Text still full of NULs means we decoded it wrongly: don't pretend it's clean.
    if "\x00" in head and head.count("\x00") > len(head) // 10:
        raise UnreadableFile(f"unrecognised text encoding (decoded as {encoding})")
    return rows


def read_sniffed(path: Path, max_rows: int) -> Rows:
    """A file whose extension isn't a spreadsheet one (.txt, .dat, none ...):
    read it as delimited data only if it is shaped like a table, else []."""
    with open(path, "rb") as f:
        raw = f.read(64 * 1024)
    encoding = _detect_encoding(path)
    if b"\x00" in raw and not encoding.startswith("utf-16") and not encoding.startswith("utf-32"):
        return []                                     # binary file
    text = raw.decode(encoding, errors="replace")
    lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
    if len(lines) < 3:
        return []
    def table_shaped(delim: str) -> bool:
        widths = [len(r) for r in csv.reader(lines, delimiter=delim)]
        common = max(set(widths), key=widths.count)
        return common >= 2 and widths.count(common) / len(widths) >= 0.8

    try:
        delimiter = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|").delimiter
    except csv.Error:
        # The sniffer gives up on padded output with ruler lines (a psql
        # query saved to a file). Try each delimiter: one that splits most
        # lines into the same number of fields is a table.
        delimiter = next((d for d in "|\t;," if table_shaped(d)), None)
        if delimiter is None:
            return []
        rows = read_delimited(path, max_rows, delimiter)
        return [[c.strip() for c in r] for r in rows]   # drop the column padding
    if not table_shaped(delimiter):
        return []                                     # prose, logs, code
    return read_delimited(path, max_rows, delimiter)


def read_csv(path: Path, max_rows: int) -> Rows:
    return read_delimited(path, max_rows)


def read_tsv(path: Path, max_rows: int) -> Rows:
    return read_delimited(path, max_rows, "\t")


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_TAG = "{%s}" % _NS["m"]


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return -1
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# Built-in Excel number formats that display a date (ECMA-376 18.8.30).
_BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))


def _date_styles(zf: zipfile.ZipFile) -> set[int]:
    """Indexes of cell styles (the `s` attribute) that format numbers as dates."""
    if "xl/styles.xml" not in zf.namelist():
        return set()
    root = ET.fromstring(zf.read("xl/styles.xml"))
    custom = {}
    for nf in root.iter(_TAG + "numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", nf.get("formatCode", ""))   # drop literals/colours
        custom[int(nf.get("numFmtId", "0"))] = bool(re.search(r"[dmy]", code, re.I))
    xfs = root.find(_TAG + "cellXfs")
    out = set()
    for i, xf in enumerate(xfs if xfs is not None else []):
        fid = int(xf.get("numFmtId", "0"))
        if fid in _BUILTIN_DATE_FMTS or custom.get(fid):
            out.add(i)
    return out


def _serial_to_date(v: str) -> str:
    """Excel stores dates as days since 1899-12-30; show them as ISO dates."""
    try:
        d = datetime(1899, 12, 30) + timedelta(days=float(v))
    except (ValueError, OverflowError):
        return v
    return d.strftime("%Y-%m-%d %H:%M:%S") if float(v) % 1 else d.strftime("%Y-%m-%d")


def read_xlsx(path: Path, max_rows: int) -> Rows:
    """Rows from every worksheet, concatenated (max_rows per sheet)."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnreadableFile(f"not a valid .xlsx archive ({e})")

    with zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.iter(_TAG + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_TAG + "t")))

        date_styles = _date_styles(zf)
        sheets = sorted(n for n in zf.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise UnreadableFile("no worksheets found")

        all_rows: Rows = []
        for sheet in sheets:
            count = 0
            with zf.open(sheet) as fh:
                for _, el in ET.iterparse(fh):
                    if el.tag != _TAG + "row":
                        continue
                    row: list[str] = []
                    for c in el.iter(_TAG + "c"):
                        idx = _col_index(c.get("r", ""))
                        t = c.get("t")
                        if t == "inlineStr":
                            val = "".join(x.text or "" for x in c.iter(_TAG + "t"))
                        else:
                            v = c.find(_TAG + "v")
                            val = v.text if v is not None and v.text else ""
                            if t == "s" and val.isdigit() and int(val) < len(shared):
                                val = shared[int(val)]
                            elif t in (None, "n") and val and c.get("s", "0").isdigit() \
                                    and int(c.get("s", "0")) in date_styles:
                                val = _serial_to_date(val)
                        if idx < 0:
                            idx = len(row)
                        while len(row) <= idx:
                            row.append("")
                        row[idx] = val
                    el.clear()
                    all_rows.append(row)
                    count += 1
                    if count >= max_rows:
                        break
            all_rows.append([])  # sheet separator (ignored by the scanner)
    return all_rows


class _HtmlTable(HTMLParser):
    """Rows of the <table>s in an HTML page ("Excel export" from many web apps)."""

    def __init__(self, max_rows: int):
        super().__init__(convert_charrefs=True)
        self.rows: Rows = []
        self.max_rows = max_rows
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr" and len(self.rows) < self.max_rows:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def read_xls(path: Path, max_rows: int) -> Rows:
    """Legacy Excel 97-2003. Files named .xls are often something else: an
    .xlsx renamed, or an HTML/CSV "Excel export" from a website. Read those
    as what they really are."""
    with open(path, "rb") as f:
        head = f.read(8)
    if xls.is_cfb(head):
        if path.stat().st_size > 200 * 1024 * 1024:     # read whole; too slow for a hook beyond this
            raise UnreadableFile("too large to inspect quickly (over 200 MB)")
        try:
            return xls.read_xls(path.read_bytes(), max_rows)
        except xls.XlsError as e:
            raise UnreadableFile(str(e))
    if head.startswith(b"PK"):
        return read_xlsx(path, max_rows)
    encoding = _detect_encoding(path)
    with open(path, "r", encoding=encoding, errors="replace") as f:
        text = f.read(4 * 1024 * 1024)
    if re.search(r"<\s*table", text, re.I):
        parser = _HtmlTable(max_rows)
        parser.feed(text)
        return parser.rows
    if "\x00" in text:
        raise UnreadableFile("unrecognised .xls content")
    return read_delimited(path, max_rows)


_ODS = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
_T, _O, _X = ("{%s}" % _ODS[k] for k in ("table", "office", "text"))
_MAX_REPEAT = 100     # "repeated" cells/rows: real data rarely repeats more than this


def _ods_cell_text(cell: ET.Element) -> str:
    kind = cell.get(_O + "value-type")
    if kind == "date":
        return (cell.get(_O + "date-value") or "").replace("T", " ")
    if kind in ("float", "percentage", "currency"):
        v = cell.get(_O + "value") or ""
        try:
            x = float(v)
            return str(int(x)) if x == int(x) and abs(x) < 1e16 else v
        except ValueError:
            return v
    return "\n".join("".join(p.itertext()) for p in cell.iter(_X + "p"))


def read_ods(path: Path, max_rows: int) -> Rows:
    """OpenDocument spreadsheet (LibreOffice): rows from every sheet."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnreadableFile(f"not a valid .ods archive ({e})")
    with zf:
        names = zf.namelist()
        if "META-INF/manifest.xml" in names and b"encryption-data" in zf.read("META-INF/manifest.xml"):
            raise UnreadableFile("password-protected spreadsheet")
        if "content.xml" not in names:
            raise UnreadableFile("no content.xml (not a spreadsheet)")
        all_rows: Rows = []
        tables = 0
        count = 0
        with zf.open("content.xml") as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                if event == "start":
                    if el.tag == _T + "table":
                        tables, count = tables + 1, 0
                    continue
                if el.tag == _T + "table-row":
                    if count < max_rows:
                        row: list[str] = []
                        for cell in el:
                            if cell.tag not in (_T + "table-cell", _T + "covered-table-cell"):
                                continue
                            reps = min(int(cell.get(_T + "number-columns-repeated", "1")), _MAX_REPEAT)
                            row += [_ods_cell_text(cell)] * reps
                        while row and not row[-1].strip():
                            row.pop()
                        if row:          # trailing blank rows are stored as one huge "repeated" row
                            reps = min(int(el.get(_T + "number-rows-repeated", "1")), _MAX_REPEAT)
                            for _ in range(min(reps, max_rows - count)):
                                all_rows.append(list(row))
                                count += 1
                    el.clear()
                elif el.tag == _T + "table":
                    all_rows.append([])  # sheet separator
                    el.clear()
        if not tables:
            raise UnreadableFile("no sheets found")
    return all_rows


READERS = {
    ".csv": read_csv,
    ".tsv": read_tsv,
    ".xlsx": read_xlsx,
    ".xlsm": read_xlsx,
    ".xls": read_xls,           # Excel 97-2003 (or an HTML/CSV export named .xls)
    ".ods": read_ods,
    ".json": lambda p, n: read_json(p, n),
    ".jsonl": lambda p, n: read_json(p, n),
    ".ndjson": lambda p, n: read_json(p, n),
}


def read_json(path: Path, max_rows: int) -> Rows:
    try:
        return jsondata.read_json(path, max_rows, _detect_encoding(path))
    except jsondata.JsonUnreadable as e:
        raise UnreadableFile(str(e))
    except jsondata.NotJson:
        return read_sniffed(path, max_rows)       # read as a table if it is one, else nothing


def read_rows(path: Path, max_rows: int) -> Rows:
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        return read_sniffed(path, max_rows)      # .txt, .dat, no extension ...
    return reader(path, max_rows)
