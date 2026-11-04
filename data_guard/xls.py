"""
Read cell values from a legacy Excel .xls file (Excel 97-2003, "BIFF8").
Standard library only.

An .xls file is an OLE2 "compound file": a small FAT file system whose
"Workbook" stream holds a sequence of BIFF records. We need just a handful:
the shared string table (SST), number formats (to recognise dates), sheet
offsets, and the cell records (text, numbers, formula results).

Spec: [MS-CFB] for the container, [MS-XLS] for the records.
"""
from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta


class XlsError(Exception):
    """The file is not a readable BIFF8 workbook (message says why)."""


_CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_END_OF_CHAIN = 0xFFFFFFFE
_FREE = 0xFFFFFFFF


def is_cfb(data: bytes) -> bool:
    return data[:8] == _CFB_MAGIC


# --------------------------------------------------------------------------- #
# Compound file (OLE2) container
# --------------------------------------------------------------------------- #
def _chain(table: list[int], start: int) -> list[int]:
    out, seen = [], set()
    s = start
    while s not in (_END_OF_CHAIN, _FREE) and s < len(table):
        if s in seen:
            raise XlsError("corrupt file (sector loop)")
        seen.add(s)
        out.append(s)
        s = table[s]
    return out


def _stream(data: bytes, names: tuple[str, ...]) -> bytes:
    """The contents of the first stream called one of `names`."""
    if len(data) < 512 or not is_cfb(data):
        raise XlsError("not an OLE2 compound file")
    sec_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    mini_size = 1 << struct.unpack_from("<H", data, 0x20)[0]
    n_fat, dir_start = struct.unpack_from("<II", data, 0x2C)
    mini_cutoff, minifat_start, n_minifat, difat_start, n_difat = struct.unpack_from("<IIIII", data, 0x38)
    if sec_size not in (512, 4096):
        raise XlsError("corrupt file (bad sector size)")

    def sector(n: int) -> bytes:
        off = (n + 1) * sec_size
        if off >= len(data):                           # the last sector may be short
            raise XlsError("corrupt file (sector out of range)")
        return data[off: off + sec_size]

    per = sec_size // 4
    # DIFAT: where the FAT sectors are (109 in the header, the rest chained).
    difat = list(struct.unpack_from("<109I", data, 0x4C))
    s, guard = difat_start, 0
    while s not in (_END_OF_CHAIN, _FREE) and guard < n_difat + 1:
        entries = struct.unpack(f"<{per}I", sector(s))
        difat += entries[:-1]
        s, guard = entries[-1], guard + 1
    fat: list[int] = []
    for fs in difat[:n_fat]:
        if fs in (_END_OF_CHAIN, _FREE):
            continue
        fat += struct.unpack(f"<{per}I", sector(fs))

    def read_chain(start: int, size: int | None = None) -> bytes:
        buf = b"".join(sector(n) for n in _chain(fat, start))
        return buf if size is None else buf[:size]

    directory = read_chain(dir_start)
    entries = []
    for off in range(0, len(directory) - 127, 128):
        e = directory[off: off + 128]
        name_len = struct.unpack_from("<H", e, 64)[0]
        name = e[: max(0, name_len - 2)].decode("utf-16-le", errors="replace")
        etype = e[66]
        start, size = struct.unpack_from("<II", e, 116)
        entries.append((name, etype, start, size))
    if not entries:
        raise XlsError("corrupt file (empty directory)")

    for name, etype, start, size in entries:
        if etype != 2 or name not in names:
            continue
        if size >= mini_cutoff:
            return read_chain(start, size)
        # Small streams live in the "mini stream", carried by the root entry.
        _, _, root_start, root_size = entries[0]
        mini_stream = read_chain(root_start, root_size)
        minifat = b"".join(sector(n) for n in _chain(fat, minifat_start)) if n_minifat else b""
        mtable = list(struct.unpack(f"<{len(minifat) // 4}I", minifat))
        return b"".join(mini_stream[n * mini_size:(n + 1) * mini_size]
                        for n in _chain(mtable, start))[:size]
    raise XlsError("no Workbook stream (not an Excel file)")


# --------------------------------------------------------------------------- #
# BIFF8 records
# --------------------------------------------------------------------------- #
_BOF, _EOF, _BOUNDSHEET, _SST, _CONTINUE = 0x0809, 0x000A, 0x0085, 0x00FC, 0x003C
_XF, _FORMAT, _DATEMODE, _FILEPASS = 0x00E0, 0x041E, 0x0022, 0x002F
_LABELSST, _LABEL, _NUMBER, _RK, _MULRK = 0x00FD, 0x0204, 0x0203, 0x027E, 0x00BD
_FORMULA, _STRING = 0x0006, 0x0207

_BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))


def _records(wb: bytes, pos: int = 0):
    """(record id, data, offset) from `pos`; CONTINUE records are yielded too."""
    n = len(wb)
    while pos + 4 <= n:
        rid, length = struct.unpack_from("<HH", wb, pos)
        yield rid, wb[pos + 4: pos + 4 + length], pos
        pos += 4 + length


class _Chunks:
    """Bytes split over a record and its CONTINUE records. A string's
    characters may break across a boundary; the next chunk then starts with
    one flag byte saying whether they continue as 8- or 16-bit."""

    def __init__(self, chunks: list[bytes]):
        self.c, self.i, self.p = chunks, 0, 0

    def _next(self):
        self.i, self.p = self.i + 1, 0
        if self.i >= len(self.c):
            raise XlsError("corrupt shared string table")

    def read(self, n: int) -> bytes:
        out = b""
        while n:
            if self.p >= len(self.c[self.i]):
                self._next()
            take = min(n, len(self.c[self.i]) - self.p)
            out += self.c[self.i][self.p: self.p + take]
            self.p += take
            n -= take
        return out

    def chars(self, count: int, high: bool) -> str:
        parts = []
        while count:
            if self.p >= len(self.c[self.i]):
                self._next()
                high = bool(self.c[self.i][0] & 1)
                self.p = 1
            width = 2 if high else 1
            take = min(count, (len(self.c[self.i]) - self.p) // width)
            if take == 0:
                self.p = len(self.c[self.i])           # malformed tail: skip to next chunk
                continue
            raw = self.c[self.i][self.p: self.p + take * width]
            parts.append(raw.decode("utf-16-le" if high else "latin-1", errors="replace"))
            self.p += take * width
            count -= take
        return "".join(parts)

    def unicode_string(self) -> str:
        """XLUnicodeRichExtendedString (SST entry)."""
        cch, flags = struct.unpack("<HB", self.read(3))
        runs = struct.unpack("<H", self.read(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<I", self.read(4))[0] if flags & 0x04 else 0
        s = self.chars(cch, bool(flags & 0x01))
        self.read(runs * 4 + ext)
        return s


def _short_string(data: bytes, off: int) -> str:
    """XLUnicodeString inside one record: u16 length, flag byte, characters."""
    cch, flags = struct.unpack_from("<HB", data, off)
    raw = data[off + 3:]
    return (raw[: cch * 2].decode("utf-16-le", errors="replace") if flags & 1
            else raw[:cch].decode("latin-1"))


def _rk(v: int) -> float:
    if v & 2:
        n = float(v >> 2 if not v & 0x80000000 else (v >> 2) - (1 << 30))
    else:
        n = struct.unpack("<d", struct.pack("<Q", (v & 0xFFFFFFFC) << 32))[0]
    return n / 100 if v & 1 else n


def _format_is_date(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)        # drop literals and colours
    return bool(re.search(r"[dmy]", code, re.I))


def _number_text(x: float, is_date: bool, epoch: datetime) -> str:
    if is_date and 0 <= x < 2_958_466:                         # up to year 9999
        d = epoch + timedelta(days=x)
        return d.strftime("%Y-%m-%d %H:%M:%S") if x % 1 else d.strftime("%Y-%m-%d")
    return str(int(x)) if x == int(x) and abs(x) < 1e16 else repr(x)


def read_xls(data: bytes, max_rows: int) -> list[list[str]]:
    """Rows from every worksheet, at most max_rows per sheet, sheets separated
    by an empty row (the same shape readers.read_xlsx returns)."""
    wb = _stream(data, ("Workbook", "Book"))
    if len(wb) < 8:
        raise XlsError("empty workbook")
    rid, bof, _ = next(_records(wb))
    if rid != _BOF:
        raise XlsError("not a BIFF workbook")
    if struct.unpack_from("<H", bof, 0)[0] != 0x0600:
        raise XlsError("Excel 5/95 or older format (save it as .xlsx or .csv)")

    # Workbook globals: everything up to the first EOF.
    sst_chunks: list[bytes] = []
    sst_count = 0
    xf_fmt: list[int] = []
    custom_date: dict[int, bool] = {}
    sheets: list[int] = []
    epoch = datetime(1899, 12, 30)
    in_sst = False
    for rid, rec, _ in _records(wb):
        if rid == _EOF:
            break
        if rid == _FILEPASS:
            raise XlsError("password-protected workbook")
        if rid == _SST:
            sst_count = struct.unpack_from("<I", rec, 4)[0] if len(rec) >= 8 else 0
            sst_chunks, in_sst = [rec[8:]], True
            continue
        if rid == _CONTINUE and in_sst:
            sst_chunks.append(rec)
            continue
        in_sst = False
        if rid == _BOUNDSHEET and len(rec) >= 6 and rec[5] == 0:   # 0 = worksheet
            sheets.append(struct.unpack_from("<I", rec, 0)[0])
        elif rid == _XF and len(rec) >= 4:
            xf_fmt.append(struct.unpack_from("<H", rec, 2)[0])
        elif rid == _FORMAT and len(rec) >= 5:
            custom_date[struct.unpack_from("<H", rec, 0)[0]] = _format_is_date(_short_string(rec, 2))
        elif rid == _DATEMODE and len(rec) >= 2 and struct.unpack_from("<H", rec, 0)[0] == 1:
            epoch = datetime(1904, 1, 1)

    sst: list[str] = []
    if sst_chunks:
        reader = _Chunks(sst_chunks)
        try:
            while len(sst) < sst_count:
                sst.append(reader.unicode_string())
        except (XlsError, struct.error):
            pass                        # truncated table: keep the strings read so far

    def is_date(xf: int) -> bool:
        if xf >= len(xf_fmt):
            return False
        f = xf_fmt[xf]
        return f in _BUILTIN_DATE_FMTS or custom_date.get(f, False)

    out: list[list[str]] = []
    for offset in sheets:
        cells: dict[int, dict[int, str]] = {}
        pending_formula: tuple[int, int] | None = None
        for rid, rec, pos in _records(wb, offset):
            if rid == _EOF:
                break
            if pos == offset and rid != _BOF:
                break                                          # bad offset
            try:
                if rid == _LABELSST:
                    r, c, _, i = struct.unpack_from("<HHHI", rec)
                    if i < len(sst):
                        cells.setdefault(r, {})[c] = sst[i]
                elif rid == _LABEL:
                    r, c = struct.unpack_from("<HH", rec)
                    cells.setdefault(r, {})[c] = _short_string(rec, 6)
                elif rid == _NUMBER:
                    r, c, xf, x = struct.unpack_from("<HHHd", rec)
                    cells.setdefault(r, {})[c] = _number_text(x, is_date(xf), epoch)
                elif rid == _RK:
                    r, c, xf, v = struct.unpack_from("<HHHI", rec)
                    cells.setdefault(r, {})[c] = _number_text(_rk(v), is_date(xf), epoch)
                elif rid == _MULRK:
                    r, c = struct.unpack_from("<HH", rec)
                    for k in range((len(rec) - 6) // 6):
                        xf, v = struct.unpack_from("<HI", rec, 4 + k * 6)
                        cells.setdefault(r, {})[c + k] = _number_text(_rk(v), is_date(xf), epoch)
                elif rid == _FORMULA:
                    r, c, xf = struct.unpack_from("<HHH", rec)
                    result = rec[6:14]
                    if result[6:8] == b"\xff\xff":
                        if result[0] == 0:                     # text: comes in the next STRING record
                            pending_formula = (r, c)
                        elif result[0] == 1:
                            cells.setdefault(r, {})[c] = "TRUE" if result[2] else "FALSE"
                    else:
                        x = struct.unpack("<d", result)[0]
                        cells.setdefault(r, {})[c] = _number_text(x, is_date(xf), epoch)
                elif rid == _STRING and pending_formula:
                    r, c = pending_formula
                    cells.setdefault(r, {})[c] = _short_string(rec, 0)
                    pending_formula = None
            except struct.error:
                continue                                       # truncated record: skip it
        for r in sorted(cells)[:max_rows]:
            row_cells = cells[r]
            row = [""] * (max(row_cells) + 1)
            for c, v in row_cells.items():
                row[c] = v
            out.append(row)
        out.append([])                                         # sheet separator
    if not sheets:
        raise XlsError("no worksheets found")
    return out
