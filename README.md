# Data Guard

**A hook that warns you before personal data in spreadsheets, JSON and data exports reaches Claude Code, so you can stop it first.**

Runs locally · Python standard library only · 18 file types · 16 kinds of personal data · tested on 200 files

> ⚠️ Data Guard is a safety net, not a guarantee. It uses heuristics and will miss some personal data. Read [What it doesn't catch](#️-what-it-doesnt-catch) before relying on it.

---

## What it does

When you or Claude are about to use a data file, Data Guard opens it **on your computer**, checks it for personal data, and asks you before anything is sent:

```
DATA PROTECTION NOTICE: personal data detected
  • exports/customers.xlsx
      – Full names (column "Customer")
      – Email addresses (column "Email")
      – Telephone numbers (column "Mobile")
Approving sends this content to Anthropic. Approve only if you are authorised to share it.
```

- **You mention a file** in a prompt (`@data/customers.csv`, `data/*.csv`): the prompt is held. Send it again within 5 minutes to go ahead, or do nothing to cancel.
- **Claude tries to read a file** with `Read`, `Grep` or `Bash`: Claude Code asks you to approve or deny. This also covers hidden file names such as `cd data && cat x.csv`, `os.path.join('data', 'x.csv')`, wildcards, and scripts that open the file themselves.
- **Clean files pass silently.** A file you confirmed by resending a prompt isn't asked about again in that session unless it changes. Approvals in Claude Code's dialog aren't remembered, because the hook can't see your answer.
- **The notice never repeats the data.** In a file without a header row, columns are shown as `column 1`, `column 2`, never as the first person's name.

## Quick start

**Requirements:** Claude Code v2.1.295 or later and Python 3.10+ (tested on 3.12). There's nothing to `pip install`.

1. Download this repository:

   ```bash
   git clone https://github.com/jimezes/data-guard-hook.git
   ```
2. Copy the `data_guard` folder into your project's `.claude/hooks/`, so you get `.claude/hooks/data_guard/guard.py`:

   ```bash
   mkdir -p /path/to/your-project/.claude/hooks
   cp -r data-guard-hook/data_guard /path/to/your-project/.claude/hooks/
   ```
3. Add the hook to your project's `.claude/settings.json`. If the file doesn't exist, create it with the block below. If it does, don't paste the block over it: keep your settings and add these two entries inside your existing `"hooks"` section.

   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [ { "type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/data_guard/guard.py\"",
             "timeout": 30, "onFailure": "block" } ] }
       ],
       "PreToolUse": [
         { "matcher": "Read|Bash|Grep",
           "hooks": [ { "type": "command",
             "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/data_guard/guard.py\"",
             "timeout": 30, "onFailure": "block" } ] }
       ]
     }
   }
   ```
4. Restart Claude Code, then run `/hooks` to check that it's loaded.
5. Check that it works, using a small fake file:

   ```bash
   cd /path/to/your-project
   cat > customers_fake.csv <<'EOF'
   Customer,Email,Mobile
   Maria Testaki,maria.testaki@mail.example,+44 7700 900101
   Nikos Fakeson,nikos.fakeson@mail.example,+44 7700 900102
   Anna Sampleton,anna.sampleton@mail.example,+44 7700 900103
   Petros Mockidis,petros.mockidis@mail.example,+44 7700 900104
   Elena Dummer,elena.dummer@mail.example,+44 7700 900105
   EOF
   python3 .claude/hooks/data_guard/guard.py --scan customers_fake.csv
   ```

   The scan should list names, emails and other kinds of data. Then start Claude Code in the project and send a prompt that mentions `@customers_fake.csv`. You should see the *Data Protection Notice* and get no answer. Delete the file when you're done.

**Other install options**

- **All your projects:** copy `data_guard` into `~/.claude/hooks/` instead, add the same block to `~/.claude/settings.json`, and replace `$CLAUDE_PROJECT_DIR/.claude` with `$HOME/.claude` in both commands. If the hook then can't run, Claude Code is blocked in every project until you fix it (see below).
- **Windows and macOS:** not tested yet. On Windows, `python3` may not exist, so you may need to change it to `python` in both commands.
- **Uninstall:** delete the `data_guard` folder and remove the two entries from your `settings.json`.

**If the hook can't run:** `"onFailure": "block"` makes Claude Code stop rather than let files through unchecked. If `python3` is missing or the hook breaks, prompts and `Read`, `Grep` and `Bash` calls in that project are blocked until you fix it. To get going without the guard, remove `"onFailure": "block"` from the settings, which means files are no longer protected when it fails.

**Useful while working:**
- Type `#allow-data` in a prompt to confirm immediately instead of resending it.
- Run `guard.py --trust path/to/fixture.csv` to stop being asked about a file you've reviewed. It's trusted only while its contents stay the same.

## What it catches

**Personal data (16 kinds)**

- **Identity & contact:** full names, first names, surnames, email addresses (also with `@` and `.` written out as `[at]` and `[dot]`), telephone numbers (international styles), postal addresses (English, German, French, Spanish, Italian, Greek), dates of birth, ages and years of birth (by column name)
- **Financial & official:** IBANs (checksum-validated), payment card numbers (Luhn-validated), national IDs (US SSN, UK NINO and Greek ΑΦΜ/ΑΜΚΑ by value; passport, tax, DNI, PESEL, NIF and others by column name)
- **Technical & location:** IP addresses, MAC addresses, precise GPS coordinates, vehicle plates and VINs
- **Special categories (GDPR Art. 9), by column name:** health, ethnicity, religion and beliefs, sexuality, union membership, biometrics, criminal records

Column names are understood in several languages (English, Greek, German, French, Spanish …) and in camelCase (`dateOfBirth`, `emailAddress`).

**File types (18)**

| | Extensions |
|---|---|
| Spreadsheets | `.xlsx` `.xlsm` `.xls` (Excel 97–2003) `.ods` (LibreOffice) |
| Delimited text | `.csv` `.tsv` |
| JSON | `.json` `.jsonl` `.ndjson`: nested records, API responses, records keyed by ID |
| Checked only if the content is laid out as a table | `.txt` `.dat` `.data` `.tab` `.psv` `.log` `.out` `.list` and files with no extension |

Text encodings: UTF-8 (with or without BOM), UTF-16/32, Windows-1252, and Greek Windows-1253 / ISO-8859-7. An `.xls` that is really an HTML table, CSV text or a renamed `.xlsx` (common with "Export to Excel" buttons) is read as what it is.

## How well it works

Measured on the project's test suite: **200 synthetic files** with a known correct answer, plus **39 end-to-end scenarios** that run the hook the way Claude Code does.

**Results per file**

| | Flagged | Not flagged |
|---|---|---|
| **Has personal data or can't be read (135)** | 128 ✅ | 7 ❌ missed |
| **Clean (65)** | 1 ❌ false alarm | 64 ✅ |

| Metric | Value |
|---|---|
| Accuracy | **96.0 %** (192 / 200) |
| Precision (flags that were right) | **99.2 %** |
| Recall (personal-data files caught) | **94.8 %** |
| Specificity (clean files left alone) | **98.5 %** |
| F1 score | **0.970** |
| Right *kind* of data named, in flagged files | **100 %** (326 / 326 expected findings) |
| Notice repeating personal data (leak checks) | **0** leaks |
| End-to-end scenarios | **38 / 39** pass |
| Time per check (incl. Python start-up) | median **255 ms**, 95th percentile 303 ms, max 391 ms |

**Variety of the test data**

| Dimension | Coverage |
|---|---|
| Files | 200: 80 with personal data, 59 clean look-alikes, 33 format edge cases, 22 former or known gaps, 6 other |
| File types | 18: `.csv` 87, `.json` 24, `.xls` 16, `.xlsx` 13, `.ods` 12, `.txt` 8, `.xlsm` 6, `.jsonl` 5, `.tsv` 4, `.ndjson` 4, `.psv` 3, `.tab` 3, `.log` 3, `.list` 3, no extension 3, `.dat` 2, `.data` 2, `.out` 2 |
| Kinds of personal data | 16; most-tested: emails (74 files), full names (71), phones (64), addresses (26), birth dates (22) |
| Languages | 9: English (UK & US), Greek, German, French, Spanish, Italian, Dutch, Polish, Portuguese |
| Encodings | UTF-8, UTF-8 with BOM, UTF-16, Windows-1252, Windows-1253 |
| Domains | HR, payroll, CRM, clinics, insurance, banking, hotels, schools, gyms, web shops, voters, devices, GPS tracks, vehicle fleets, logs |
| Traps (clean, but look risky) | product, colour and football-team names, ISBNs, epoch timestamps, order dates, team mailboxes, placeholder people, data dictionaries, JSON schemas, map files |
| Shapes | multi-sheet workbooks, 40-column sheets, sparse columns, 600 placeholder rows before the data, files without headers, `field,value` records, nested and keyed JSON, quoted line breaks |

> **Read these numbers with care.** All 200 files are synthetic, written by the same project that wrote the hook, and the 8 misses are known and documented. This is a regression suite, not an independent benchmark, and its test files aren't published in this repository. Real exports from your own systems are messier, so check a few of your own files with `--scan` first.

## ⚠️ What it doesn't catch

Data Guard reduces risk. It does **not** make sharing data safe or compliant on its own.

- **Known misses in testing:**
  - space-separated logs (Apache/nginx style)
  - one-column lists in plain-text files (one email per line)
  - Dutch, Polish and Portuguese addresses under a neutral column name
  - names inside free-text sentences
- **By design, asks more than needed:** any column literally named `Name` is checked, so a product list with a "Name" column will prompt you.
- **Can't be inspected, so you're asked:** password-protected files, Excel 95 `.xls`, `.xls`/`.ods` over 200 MB, broken JSON.
- **Too much to check in time:** the check stops after `max_check_seconds` (20) and asks you, because the files may hold personal data that wasn't reached.
- **Can't be seen at all:** a file name typed in while a script runs, or built from values the hook can't see.
- **Very long unbroken text:** a prompt or Bash command with a long stretch of text and no spaces, such as pasted base64, slows the check down. 20,000 characters took about 10 seconds. At 40,000 the check ran past its time limit and Claude Code blocked the prompt. Resending is likely to fail the same way, so break the text across lines or put it in a file. Text with spaces or line breaks isn't affected: 100,000 characters took under a second.
- **Very long files:** only the first 20,000 rows are read (`max_rows`), and later rows aren't checked. You get no warning about this, so personal data that starts after row 20,000 passes silently. In a 500,000-row test, a file with people only after row 400,000 was missed. A file with people from the top was caught in under 2 seconds.
- **Folder-wide Bash commands:** a command that names only a folder, or finds its files while it runs, isn't checked, so data can pass with no notice. Examples: `grep -r gmail data`, `rg gmail`, `find . -name "*.csv" -exec cat {} +`, `find data -name "*.csv" | xargs head`, `tar cz data`. Claude's `Grep` tool over a folder is checked. A Bash command that names the file or a wildcard, such as `cat data/*.csv`, is too.
- **Only `Read`, `Grep` and `Bash` are guarded.** Other tools (MCP servers, web fetches) aren't.
- **Files in `skip_file_names`** (`package.json`, `tsconfig.json`, …) and paths in `allowlist` are never checked.
- **Limited real-world testing:** holding an `@`-mentioned file was confirmed once, on Linux with Claude Code 2.1.317, using a session log rather than network traffic. Files saved by real Excel or LibreOffice, and Windows and macOS, haven't been tested.
- **Wildcards:** only the first 2000 matches are considered, with no warning.
- **Approving sends the data.** The decision, and the responsibility, stay with you.

## Configuration

Settings live in `data_guard/config.json` (once installed: `.claude/hooks/data_guard/config.json`):

| Setting | What it does |
|---|---|
| `tool_action` | `"ask"` (default) shows the approval dialog; `"deny"` blocks outright |
| `extensions`, `sniff_extensions` | Which file types are checked (always, or only when shaped like a table) |
| `skip_file_names` | Tool/config files never checked (e.g. `package.json`) |
| `max_rows`, `max_values_per_column` | How much of each file is sampled |
| `max_files_per_search` | How many files a folder search or wildcard covers (default 2000). A folder search that goes over warns you |
| `max_check_seconds` | How long a check may run before it stops and asks you (default 20). Keep it below the hook's `timeout` in `settings.json` |
| `confirmation_window_seconds` | How long a held prompt can be confirmed by resending (default 300) |
| `override_token` | The word that confirms a prompt immediately (default `#allow-data`) |
| `allowlist`, `trusted_files` | Paths or reviewed files to skip (prefer `--trust`: it's tied to the file contents) |

## Extending it

Everything is plain Python. Each change has one place to go:

- **A new kind of personal data:** append a `Detector` to `DETECTORS` in `detectors.py`. A column is flagged if its **name** matches `header` or enough of its **values** pass `value`:

  ```python
  Detector(
      name="National insurance numbers",               # label shown in the notice
      header=r"(^|_)(nino|ni_?number)($|_)",           # normalised column name
      value=lambda v: bool(re.fullmatch(r"[A-Z]{2}\d{6}[A-D]", v.replace(" ", ""))),
      min_hits=2, min_ratio=0.5,
  )
  ```
- **Another language:** add its column names to a detector's `header` pattern (e.g. `τηλεφωνο` for phone), or its street words to `_STREET_SUFFIX`.
- **A new file type:** write `read_<type>(path, max_rows)` returning rows of strings, register it in `READERS` in `readers.py`, and add the extension to `config.json`.

## Project layout

```
data-guard-hook/
├── README.md                      this file
├── LICENSE
└── data_guard/                    the hook: copy this folder into .claude/hooks/
    ├── guard.py                   entry point: finds file paths, scans, shows the notice
    ├── detectors.py               what counts as personal data
    ├── readers.py, xls.py, jsondata.py   file formats
    └── config.json                settings
```

## License

MIT. See [LICENSE](LICENSE).

Built with the help of Claude Code. This is an independent project, not an official Anthropic product.
