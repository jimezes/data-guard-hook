#!/usr/bin/env python3
"""
Data guard: a Claude Code hook that checks spreadsheets/CSVs for personal data
before their contents can be sent to Claude.

Hook events handled:
  UserPromptSubmit  You reference a file (e.g. @data/x.csv). The first
                    submission is held and a data protection notice is shown;
                    submitting again within the confirmation window proceeds.
  PreToolUse        Claude is about to Read/Grep/Bash a file. Claude Code shows
                    its confirmation dialog with the notice (or denies, per
                    config "tool_action"). Files you already confirmed in this
                    session are not flagged again unless they change.

Manual use (no hook needed):
  python3 .claude/hooks/data_guard/guard.py --scan path/to/file.csv [...]
  python3 .claude/hooks/data_guard/guard.py --trust path/to/fixture.csv [...]
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from detectors import (DETECTORS, is_placeholder, key_value_rows,  # noqa: E402
                       looks_like_single_name, norm_header, safe_labels,
                       split_name_columns)
from readers import UnreadableFile, read_rows                       # noqa: E402

CONFIG_PATH = HERE / "config.json"
CONFIG = json.loads(CONFIG_PATH.read_text())
EXTS = tuple(e.lower() for e in CONFIG["extensions"])
# Other files are read only if they turn out to be shaped like a table.
SNIFF = tuple(e.lower() for e in CONFIG.get("sniff_extensions", []))
GUARDED = EXTS + tuple(e for e in SNIFF if e)
PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HERE.parents[2]).resolve()
STATE_DIR = Path(tempfile.gettempdir()) / "claude-data-guard"


# Well-known tool/config files (package.json, tsconfig.json ...) never hold
# personal data; skipping them keeps everyday commands fast and quiet.
SKIP_NAMES = tuple(CONFIG.get("skip_file_names", []))


def _skipped_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), g.lower()) for g in SKIP_NAMES)


def _is_candidate(p: Path) -> bool:
    ext = p.suffix.lower()
    return (ext in GUARDED or (ext == "" and "" in SNIFF)) and not _skipped_name(p.name)


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    detector: str
    column: str
    reason: str          # "header", "N/M values", "split across two columns" ...
    where: str = ""      # display override, e.g. "row 'email'"


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def flagged(self) -> bool:
        return bool(self.findings) or self.error is not None


def _split_tables(rows: list[list[str]]) -> list[list[list[str]]]:
    """xlsx sheets are separated by an empty row marker; csv is one table."""
    tables, cur = [], []
    for r in rows:
        if r == [] and cur:
            tables.append(cur)
            cur = []
        elif r:
            cur.append(r)
    if cur:
        tables.append(cur)
    return tables


def _collect_columns(data: list[list[str]], ncols: int) -> list[list[str]]:
    """Non-empty values per column. Stops collecting a column once it holds
    `max_values_per_column` real (non-placeholder) values, so a file whose
    first 500 rows are "N/A" is still read far enough to reach the data."""
    cap = CONFIG.get("max_values_per_column", 500)
    columns: list[list[str]] = [[] for _ in range(ncols)]
    real = [0] * ncols
    for r in data:
        for ci in range(min(len(r), ncols)):
            if real[ci] >= cap:
                continue
            v = r[ci].strip()
            if v:
                columns[ci].append(v)
                if not is_placeholder(v):
                    real[ci] += 1
        if all(n >= cap for n in real):
            break
    return columns


def scan_table(rows: list[list[str]]) -> list[Finding]:
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    ncols = max(len(r) for r in rows)
    labels = [header[ci] if ci < len(header) and header[ci].strip() else f"column {ci + 1}"
              for ci in range(ncols)]
    norms = [norm_header(h) for h in labels]
    columns = _collect_columns(data, ncols)
    findings: list[Finding] = []

    for ci in range(ncols):
        col_name, norm = labels[ci], norms[ci]
        real = [v for v in columns[ci] if not is_placeholder(v)]
        if not real:
            continue
        for det in DETECTORS:
            if det.skip_column and det.skip_column(norm, norms):
                continue
            if det.header_matches(norm):
                findings.append(Finding(det.name, col_name, "header"))
                continue
            if det.value is None:
                continue
            matched = [v for v in real if det.value(v)]
            hits = len(matched)
            if (hits >= det.min_hits
                    and hits / len(real) >= det.min_ratio
                    and len(set(matched)) / hits >= det.min_distinct
                    and (det.column_check is None or det.column_check(matched))):
                findings.append(Finding(det.name, col_name, f"{hits}/{len(real)} values"))

    # Personal data visible only across columns / rows.
    named = {f.column for f in findings if f.detector in ("First names", "Surnames", "Full names")}
    split = [(name, cols, reason) for name, cols, reason in split_name_columns(labels, columns)
             if not named & set(cols)]

    # The notice shows column names, which come from the first row. In a file
    # without a header row that row is a person: never repeat it. A label is
    # hidden when it looks like personal data itself, or when it passes the
    # very value check that flagged its own column (a name heading a
    # column of names), or when it heads a name column found by shape.
    shown = safe_labels(labels)
    by_value = {(f.column, f.detector) for f in findings if f.reason != "header"}
    det_by_name = {d.name: d for d in DETECTORS}
    for ci, lab in enumerate(labels):
        for col, dname in by_value:
            d = det_by_name.get(dname)
            if col == lab and d and d.value and d.value(lab.strip()):
                shown[ci] = f"column {ci + 1}"
    for _, cols, _ in split:
        for c in cols:
            ci = labels.index(c)
            if looks_like_single_name(c.strip()):
                shown[ci] = f"column {ci + 1}"
    show = {lab: shown[i] for i, lab in reversed(list(enumerate(labels)))}   # first occurrence wins

    findings = [Finding(f.detector, show.get(f.column, f.column), f.reason, f.where) for f in findings]
    for name, cols, reason in split:
        cols = [show.get(c, c) for c in cols]
        findings.append(Finding(name, " + ".join(cols), reason,
                                where=" + ".join(_quote(c) for c in cols)))
    for name, cols, reason in key_value_rows(labels, columns, data[:5000]):
        findings.append(Finding(name, cols[0], reason, where=cols[0]))
    return findings


def scan_file(path: Path) -> FileReport:
    report = FileReport(path)
    try:
        for table in _split_tables(read_rows(path, CONFIG["max_rows"])):
            report.findings.extend(scan_table(table))
    except UnreadableFile as e:
        report.error = str(e)
    except Exception as e:  # never let a parsing bug silently let data through
        report.error = f"could not inspect ({type(e).__name__}: {e})"
    return report


# Scan results are cached per file version (path+size+mtime) and per version
# of the guard's own code/config, so repeated Greps over a folder stay fast.
_CACHE_PATH = STATE_DIR / "scan-cache.json"
_FINGERPRINT = hashlib.sha256(b"".join(
    (HERE / n).read_bytes() for n in ("guard.py", "detectors.py", "readers.py", "xls.py", "jsondata.py", "config.json")
)).hexdigest()[:16]
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_CACHE_PATH.read_text())
            _cache = data["entries"] if data.get("fingerprint") == _FINGERPRINT else {}
        except (OSError, ValueError, KeyError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        entries = dict(list(_cache.items())[-2000:])
        _CACHE_PATH.write_text(json.dumps({"fingerprint": _FINGERPRINT, "entries": entries}))
    except OSError:
        pass


def scan_file_cached(path: Path) -> FileReport:
    cache, key = _load_cache(), _file_key(path)
    hit = cache.get(key)
    if hit is not None:
        return FileReport(path, [Finding(*f) for f in hit["findings"]], hit["error"])
    r = scan_file(path)
    cache[key] = {"findings": [[f.detector, f.column, f.reason, f.where] for f in r.findings],
                  "error": r.error}
    return r


# --------------------------------------------------------------------------- #
# Finding file paths in prompts / tool calls
# --------------------------------------------------------------------------- #
_EXT_RE = "|".join(re.escape(e.lstrip(".")) for e in GUARDED)
_PATH_RE = re.compile(
    rf"""(?:"([^"\n]+\.(?:{_EXT_RE}))"|'([^'\n]+\.(?:{_EXT_RE}))'|([^\s"'`<>|;&()]+\.(?:{_EXT_RE})))(?![\w])""",
    re.I,
)
# Any wildcard token (e.g. "data/*", "x.c*", "*.[ct]sv"): expand it and keep
# whatever guarded files it matches, since the extension may not be spelled out.
_GLOB_RE = re.compile(r"""(?:"([^"\n]*[*?\[][^"\n]*)"|'([^'\n]*[*?\[][^'\n]*)'|([^\s"'`<>|;&()]*[*?\[][^\s"'`<>|;&()]*))""")


def _resolve(raw: str, cwd: Path) -> list[Path]:
    raw = os.path.expanduser(raw.lstrip("@"))
    p = Path(raw) if os.path.isabs(raw) else cwd / raw
    if any(ch in raw for ch in "*?["):
        limit = CONFIG.get("max_files_per_search", 2000)
        try:
            return [Path(m) for m in itertools.islice(glob.iglob(str(p), recursive=True), limit)]
        except (OSError, RuntimeError):      # unreadable folder, symlink loop
            return []
    return [p]


def _resolve_one(raw: str, cwd: Path) -> Path:
    """The path `raw` names. A wildcard that matches nothing stays as written."""
    matches = _resolve(raw, cwd)
    return matches[0] if matches else cwd / os.path.expanduser(raw.lstrip("@"))


def paths_in_text(text: str, cwd: Path, quoted_globs_only: bool = False) -> list[Path]:
    out = []
    for m in _PATH_RE.finditer(text or ""):
        out.extend(_resolve(next(g for g in m.groups() if g), cwd))
    for m in _GLOB_RE.finditer(text or ""):
        if quoted_globs_only and m.group(3):
            continue        # in source code a bare "*" is multiplication, "**" a power
        out.extend(p for p in _resolve(next(g for g in m.groups() if g), cwd) if _is_candidate(p))
    return out


_INTERPRETER_RE = re.compile(
    r"""(?:^|[;&|(]|\s)(?:sudo\s+|uv\s+run\s+|poetry\s+run\s+|pipenv\s+run\s+)?"""
    r"""(python[\d.]*|node|nodejs|deno(?:\s+run)?|bun|ts-node|tsx|ruby|perl|php|Rscript|julia|"""
    r"""bash|sh|zsh|source|\.)\s+((?:-[\w-]+\s+)*)("[^"]+"|'[^']+'|[^\s;&|)]+)""")
_LOCAL_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\.?[\w.]+)\s+import|import\s+([\w.]+))", re.M)
_EXT_WORD_RE = re.compile(rf"\.?\b({'|'.join(re.escape(e.lstrip('.')) for e in EXTS)})\b", re.I)


def _scripts_run_by(cmd: str, bases: list[Path]) -> list[Path]:
    """Script files a command runs: `python3 analyse.py`, `node x.js`, `bash run.sh`,
    `python -m pkg.report` (-> pkg/report.py)."""
    out = []
    for m in _INTERPRETER_RE.finditer(cmd):
        # "." runs a script only as a command of its own ". env.sh". In
        # "find . -name x" or "ls . y" it is just a folder.
        before = cmd[:m.start(1)].rstrip(" \t")
        if (m.group(1) == "." and before and before[-1] not in "\n;&|("
                and not before.endswith(("then", "do", "else"))):
            continue
        flags, target = m.group(2), m.group(3).strip("\"'")
        if flags.rstrip().endswith("-c"):
            continue                                  # inline code: already part of cmd
        if flags.rstrip().endswith("-m"):
            target = target.replace(".", "/")
            names = [target + ".py", target + "/__main__.py"]
        else:
            names = [target]
        for b in bases:
            for n in names:
                p = _resolve_one(n, b)
                if p.is_file() and not _is_candidate(p):
                    out.append(p)
    return out


def _local_imports(script: Path, text: str) -> list[Path]:
    """Modules a Python script imports from its own folder (helpers it may
    delegate the file reading to)."""
    out = []
    for m in _LOCAL_IMPORT_RE.finditer(text):
        mod = (m.group(1) or m.group(2)).lstrip(".").replace(".", "/")
        for cand in (script.parent / f"{mod}.py", script.parent / mod / "__init__.py"):
            if cand.is_file():
                out.append(cand)
    return out


def paths_in_bash(cmd: str, cwd: Path, _depth: int = 0, _seen: set | None = None,
                  _extra_bases: list[Path] | None = None) -> list[Path]:
    """Like paths_in_text, plus the usual ways a command hides a file name:
    'data/customers' + '.csv'      string pieces glued together
    cd data && cat customers.csv   a file named relative to a `cd`
    os.path.join('data', 'x.csv')  folder and file name in separate strings
    os.listdir('data') ... '.csv'  a folder plus an extension: every such file in it
    python3 analyse.py             the script (and local modules it imports) is read
                                   and searched the same way, two levels deep."""
    seen = _seen if _seen is not None else set()
    cmd = re.sub(r"""(['"])\s*\+\s*\1""", "", cmd or "")          # 'a' + '.csv' -> 'a.csv'
    bases = [cwd] + [b for b in (_extra_bases or []) if b != cwd]
    for m in re.finditer(r"""(?:^|[;&|(]|\s)(?:cd|pushd)\s+("[^"]+"|'[^']+'|[^\s;&|)]+)""", cmd):
        target = m.group(1).strip("\"'")
        bases.append(_resolve_one(target, bases[-1]))
    # every quoted string (also ones nested inside "python -c '...'") and every
    # bare word; inside a script file only strings, since bare words are code.
    quoted = re.findall(r"'([^'\n]{1,300})'", cmd) + re.findall(r'"([^"\n]{1,300})"', cmd)
    bare = [] if _depth else re.findall(r"""[^\s"'`<>|;&(),=]{1,300}""", cmd)
    tokens = [t for t in dict.fromkeys(quoted + bare) if t.strip()][:2000]
    named_dirs: list[tuple[Path, str]] = []   # (folder, the token that named it)
    for _ in range(2):                          # folder tokens, also nested ones
        for t in tokens:
            if len(bases) >= 20 or any(ch in t for ch in "*?[$") or len(t) > 300:
                continue
            for b in list(bases):
                d = _resolve(t, b)[0]
                if d.is_dir() and d not in bases and not _contains_project(d):
                    bases.append(d)
                    named_dirs.append((d, t))
    out: list[Path] = []
    for b in bases:
        out += paths_in_text(cmd, b, quoted_globs_only=_depth > 0)
        for t in tokens:                        # a name missing only its extension
            if any(ch in t for ch in "*?[$") or len(t) > 300 or Path(t).suffix.lower() in GUARDED:
                continue
            p = _resolve(t, b)[0]
            out += [Path(f"{p}{e}") for e in EXTS if Path(f"{p}{e}").is_file()]
    # A folder named together with a spreadsheet extension, in the same
    # statement: code that lists the folder and filters by extension
    # (os.listdir(d) ... f.endswith('.csv')). Folders that contain the project
    # itself ("/", your home) never count.
    for m in _EXT_WORD_RE.finditer(cmd if named_dirs else ""):
        window = cmd[max(0, m.start() - 300): m.end() + 300]
        ext = "." + m.group(1).lower()
        deep = bool(re.search(r"\b(walk|rglob|recursive)\b|\*\*", window))
        for d, tok in named_dirs:
            if tok in window:
                files = _walk_candidates(d) if deep else [p for p in d.iterdir() if p.is_file()]
                out += [p for p in files if p.suffix.lower() == ext]
    # Scripts the command runs, and local modules they import.
    if _depth < 2:
        for script in _scripts_run_by(cmd, bases):
            if script in seen:
                continue
            seen.add(script)
            try:
                if script.stat().st_size > 1_000_000:
                    continue
                text = script.read_text(errors="replace")
            except OSError:
                continue
            inner = [script.parent] + bases
            out += paths_in_bash(text, cwd, _depth + 1, seen, inner)
            for mod in _local_imports(script, text):
                if mod not in seen:
                    seen.add(mod)
                    out += paths_in_bash(mod.read_text(errors="replace"), cwd, _depth + 1, seen,
                                         [mod.parent] + bases)
    return out


def _contains_project(d: Path) -> bool:
    """True for "/", your home folder and the project folder itself."""
    try:
        d = d.resolve()
        return d == PROJECT_DIR or d in PROJECT_DIR.parents
    except OSError:
        return True


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    return [x for alt in m.group(1).split(",")
            for x in _expand_braces(pattern[:m.start()] + alt + pattern[m.end():])]


_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages"}


_UNVERIFIED: list[tuple[Path, str]] = []      # (folder, why) found while collecting paths


def _walk_candidates(root: Path, limit: int | None = None) -> list[Path]:
    limit = limit or CONFIG.get("max_files_per_search", 2000)
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in _SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1].lower() in GUARDED and not _skipped_name(f):
                p = Path(d) / f
                out.append(p)
                if len(out) >= limit:
                    # Too many to check: ask rather than let the rest through unseen.
                    _UNVERIFIED.append((root, f"more than {limit} data files below this folder; "
                                              "not all could be checked (narrow the search with a glob)"))
                    return out
    return out


def paths_in_tool(tool: str, tin: dict, cwd: Path) -> list[Path]:
    if tool == "Read":
        return _resolve(tin.get("file_path", ""), cwd)
    if tool == "Bash":
        return paths_in_bash(tin.get("command", ""), cwd)
    if tool == "Grep":
        target = _resolve_one(tin.get("path") or ".", cwd)
        if target.is_file():
            return [target]
        if not target.is_dir():
            return []
        # Without a glob/type, Grep searches every file below the folder,
        # spreadsheets included. With one, only the files it selects.
        patterns = _expand_braces(tin.get("glob") or "")
        ftype = (tin.get("type") or "").lower()
        out = []
        for p in _walk_candidates(target):
            rel = p.relative_to(target).as_posix()
            if ftype and p.suffix.lower() != f".{ftype}":
                continue
            if patterns != [""] and not any(
                    fnmatch.fnmatch(p.name, g) or fnmatch.fnmatch(rel, g)
                    or fnmatch.fnmatch(p.name, g.rsplit("/", 1)[-1]) for g in patterns):
                continue
            out.append(p)
        return out
    return []


def _allowlisted(p: Path) -> bool:
    """Opt-in path globs. Trusts the path, not the contents: prefer --trust."""
    try:
        rel = "/" + str(p.resolve().relative_to(PROJECT_DIR))
    except ValueError:
        rel = str(p.resolve())
    return any(fnmatch.fnmatch(rel, "/" + g.lstrip("/")) or fnmatch.fnmatch(rel, g)
               for g in CONFIG.get("allowlist", []))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _trusted(p: Path) -> bool:
    """Files reviewed with --trust: skipped only while their contents are unchanged."""
    trusted = CONFIG.get("trusted_files") or {}
    return bool(trusted) and _sha256(p) in trusted


def check_paths(paths: list[Path]) -> list[FileReport]:
    seen, reports = set(), []
    for folder, why in dict.fromkeys(_UNVERIFIED):
        reports.append(FileReport(folder, error=why))
    _UNVERIFIED.clear()
    for p in paths:
        p = p.resolve()
        if p in seen or not p.is_file() or not _is_candidate(p) or _allowlisted(p) or _trusted(p):
            continue
        seen.add(p)
        r = scan_file_cached(p)
        if r.flagged:
            reports.append(r)
    _save_cache()
    return reports


# --------------------------------------------------------------------------- #
# Session state: pending confirmations and confirmed files
# A file is identified by path + size + mtime, so editing it re-triggers checks.
# --------------------------------------------------------------------------- #
def _file_key(p: Path) -> str:
    st = p.stat()
    return f"{p}|{st.st_size}|{st.st_mtime_ns}"


def _state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "default")
    return STATE_DIR / f"{safe}.json"


def _load_state(session_id: str) -> dict:
    try:
        return json.loads(_state_path(session_id).read_text())
    except (OSError, ValueError):
        return {"pending": {}, "approved": []}


def _save_state(session_id: str, state: dict) -> None:
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        _state_path(session_id).write_text(json.dumps(state))
    except OSError:
        pass  # without state the user is simply asked again, which is safe


def _unapproved(reports: list[FileReport], state: dict) -> list[FileReport]:
    approved = set(state.get("approved", []))
    return [r for r in reports if _file_key(r.path) not in approved]


# --------------------------------------------------------------------------- #
# Notice text
# --------------------------------------------------------------------------- #
RULE = "─" * 64


def _display(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_DIR))
    except ValueError:
        return str(p)


_POSITIONAL = re.compile(r"^column \d+$")


def _quote(label: str) -> str:
    """'column "Email"' for a real column name, 'column 3' for a position."""
    return label if _POSITIONAL.match(label) else f'column "{label}"'


def summarise(reports: list[FileReport]) -> str:
    lines = []
    for r in reports:
        lines.append(f"  • {_display(r.path)}")
        if r.error:
            lines.append(f"      – Contents could not be verified: {r.error}")
            continue
        by_kind: dict[str, tuple[list[str], list[str]]] = {}
        for f in r.findings:
            cols, other = by_kind.setdefault(f.detector, ([], []))
            if f.where:
                other.append(f.where)
            else:
                cols.append(_quote(f.column))
        for kind, (cols, other) in by_kind.items():
            parts = []
            cols = list(dict.fromkeys(cols))
            if cols:
                parts.append(", ".join(cols))
            parts += list(dict.fromkeys(other))
            lines.append(f"      – {kind} ({'; '.join(parts)})")
    return "\n".join(lines)


def prompt_notice(reports: list[FileReport]) -> str:
    window = CONFIG.get("confirmation_window_seconds", 300) // 60
    return f"""
{RULE}
DATA PROTECTION NOTICE: personal data detected
{summarise(reports)}

Proceeding sends this content to Anthropic. Continue only if you are
authorised to share it and it complies with your data policy (e.g. GDPR).
Your prompt is held; nothing has been sent. To proceed, resubmit it
within {window} minutes.
{RULE}""".strip("\n")


def tool_notice(reports: list[FileReport]) -> str:
    return (
        "DATA PROTECTION NOTICE: personal data detected\n"
        f"{summarise(reports)}\n"
        "Approving sends this content to Anthropic. Approve only if you are "
        "authorised to share it."
    )


# --------------------------------------------------------------------------- #
# Hook handlers
# --------------------------------------------------------------------------- #
def on_prompt(event: dict, cwd: Path) -> dict | None:
    session = event.get("session_id", "")
    prompt = event.get("prompt", "")
    state = _load_state(session)
    reports = _unapproved(check_paths(paths_in_text(prompt, cwd)), state)
    if not reports:
        return None

    keys = sorted(_file_key(r.path) for r in reports)
    token = hashlib.sha256("\n".join(keys).encode()).hexdigest()
    now = time.time()
    window = CONFIG.get("confirmation_window_seconds", 300)
    state["pending"] = {k: t for k, t in state.get("pending", {}).items() if now - t < window}

    confirmed = token in state["pending"] or CONFIG["override_token"] in prompt
    if confirmed:
        state["pending"].pop(token, None)
        state["approved"] = sorted(set(state.get("approved", [])) | set(keys))
        _save_state(session, state)
        return None

    state["pending"][token] = now
    _save_state(session, state)
    return {"decision": "block", "reason": prompt_notice(reports)}


def on_tool(event: dict, cwd: Path) -> dict | None:
    state = _load_state(event.get("session_id", ""))
    reports = check_paths(paths_in_tool(event.get("tool_name", ""),
                                        event.get("tool_input") or {}, cwd))
    if CONFIG.get("remember_confirmation_for_session", True):
        reports = _unapproved(reports, state)
    if not reports:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": CONFIG.get("tool_action", "ask"),
        "permissionDecisionReason": tool_notice(reports),
    }}


# --------------------------------------------------------------------------- #
# Time limit
# Claude Code cancels a hook at its `timeout` and, unless the hook is set to
# block on failure, lets the action go ahead. So the guard stops itself first
# and asks, naming what it couldn't finish. A timer thread also covers a single
# slow file, which a check between files would miss.
# --------------------------------------------------------------------------- #
_finish_lock = threading.Lock()
_finished = False


def _finish(out: dict | None) -> None:
    """Print the hook's one answer; later calls do nothing."""
    global _finished
    with _finish_lock:
        if _finished:
            return
        _finished = True
        if out:
            print(json.dumps(out), flush=True)


def _incomplete_prompt(event: dict, seconds: int) -> dict | None:
    """Hold the prompt once. Resubmitting goes ahead, as for a normal notice."""
    prompt = event.get("prompt", "")
    if CONFIG["override_token"] in prompt:
        return None
    session = event.get("session_id", "")
    state = _load_state(session)
    token = "incomplete:" + hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()
    now = time.time()
    window = CONFIG.get("confirmation_window_seconds", 300)
    state["pending"] = {k: t for k, t in state.get("pending", {}).items() if now - t < window}
    if token in state["pending"]:
        state["pending"].pop(token)
        _save_state(session, state)
        return None
    state["pending"][token] = now
    _save_state(session, state)
    return {"decision": "block", "reason": f"""
{RULE}
DATA PROTECTION NOTICE: check incomplete
The files in this prompt could not all be checked within {seconds} seconds, so
they may hold personal data. Name fewer files, or resubmit within
{window // 60} minutes to continue without a complete check.
{RULE}""".strip("\n")}


def _incomplete_tool(seconds: int) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": CONFIG.get("tool_action", "ask"),
        "permissionDecisionReason": (
            f"DATA PROTECTION NOTICE: check incomplete. The files could not all be "
            f"checked within {seconds} seconds, so they may hold personal data. "
            "Approving sends this content to Anthropic. Narrow the search to fewer "
            "files, or approve only if you are authorised to share it."),
    }}


def _out_of_time(name: str, event: dict, seconds: int) -> None:
    global _finished
    with _finish_lock:
        if _finished:
            return
        _finished = True
        try:
            _save_cache()              # keep progress, so a retry gets further
        except Exception:
            pass                       # the scan is still using the cache
        try:
            out = _incomplete_prompt(event, seconds) if name == "UserPromptSubmit" else _incomplete_tool(seconds)
        except Exception as e:
            out = _incomplete_tool(seconds) if name == "PreToolUse" else {
                "decision": "block",
                "reason": f"DATA PROTECTION NOTICE: check incomplete ({type(e).__name__})."}
        if out:
            print(json.dumps(out), flush=True)
        os._exit(0)                    # the scan thread can't be stopped any other way


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--trust":
        # Pin reviewed files (e.g. synthetic fixtures) by content hash.
        trusted = CONFIG.setdefault("trusted_files", {})
        for a in sys.argv[2:]:
            p = Path(a).resolve()
            trusted[_sha256(p)] = _display(p)
            print(f"Trusted while unchanged: {_display(p)}")
        CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n")
        return 0

    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        reports = [scan_file(Path(a)) for a in sys.argv[2:]]
        flagged = [r for r in reports if r.flagged]
        print(summarise(flagged) if flagged else "No personal data detected.")
        return 1 if flagged else 0

    event = json.load(sys.stdin)
    name = event.get("hook_event_name")
    cwd = Path(event.get("cwd") or PROJECT_DIR)

    seconds = CONFIG.get("max_check_seconds", 20)
    timer = threading.Timer(seconds, _out_of_time, (name, event, seconds))
    timer.daemon = True
    timer.start()

    try:
        handler = {"UserPromptSubmit": on_prompt, "PreToolUse": on_tool}.get(name)
        _finish(handler(event, cwd) if handler else None)
    except Exception as e:
        # Fail closed: a guard bug should ask, not silently allow.
        msg = (f"DATA PROTECTION NOTICE: check incomplete ({type(e).__name__}: {e}). "
               f"Review the file(s) before proceeding.")
        if name == "PreToolUse":
            _finish({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": msg}})
        else:
            _finish({"decision": "block", "reason": msg})
    return 0


if __name__ == "__main__":
    sys.exit(main())
