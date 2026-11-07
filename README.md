# Bulk Issue Import

Creates GitHub issues in bulk from the Master Bug Tracker, then backfills the
tracker's `GITHUB STATUS` and `GITHUB LINK` columns with the result.

The spreadsheet still owns the formatting. Its `REPORT TITLE` and `BUG REPORT`
columns already assemble the issue title and body, and this tool carries them
across unchanged — so QA edits the report template in the sheet, not in code.

## Setup

```powershell
winget install --id GitHub.cli
gh auth login                    # needs the repo scope
gh auth refresh -s project       # only if you will use --project
pip install -r requirements.txt  # only for export_tracker_csv.py
```

## Getting CSVs

One CSV per tab. From the live tracker: open the tab, **File > Download >
Comma-separated values**. From an archived `.xlsx` copy:

```powershell
python export_tracker_csv.py "Master Bug Tracker.xlsx" --out-dir csv
python export_tracker_csv.py "MASTER Bug Tracker ARCHIVED.xlsx" --out-dir csv-archived
```

Both trackers have a `Game` tab, a `Tablet` tab and so on. Keep them in separate
directories and you can still import from both at once — where file names
collide, the importer labels rows by directory (`csv--Game.csv`,
`csv-archived--Game.csv`) so every row is traceable to one file. `--prefix
archived` does the same at export time if you would rather they share a
directory.

A CSV needs only four columns — `REPORT TITLE`, `BUG REPORT`, `GITHUB STATUS`,
`GITHUB LINK`. They are found by name, so tabs can differ in everything else.

## Running it

Always dry run first. It renders every issue it would create to
`import-logs/dry-run.md` and touches nothing:

```powershell
python -m bugimport csv/*.csv --dry-run
```

Read that file, then import for real:

```powershell
python -m bugimport csv/*.csv --repo alterea/aoi
```

Start small the first time — `--limit 3` creates three issues, so you can check
how they look on GitHub before committing to the rest.

Then paste the two backfilled columns back into the tracker, or re-upload the
CSVs. (The Google Sheets source will remove this step; the CLI stays the same.)

### Options

| Flag | Effect |
| --- | --- |
| `--repo OWNER/NAME` | target repository; required unless `--dry-run` |
| `--dry-run` | render every issue, create nothing, modify nothing |
| `--limit N` | create at most N issues this run; also caps what `--dry-run` renders |
| `--label NAME` | apply a label; repeatable, and it must already exist |
| `--project TITLE` | add issues to a project |
| `--project-status TEXT` | set each issue's project status, e.g. `"To Be Organized"`; needs `--project` |
| `--project-field NAME` | project field `--project-status` writes to (default `Status`) |
| `--status TEXT` | value written to `GITHUB STATUS` (default `LOGGED`) |
| `--sleep SECONDS` | pause between creates (default `1.0`) |
| `--out-dir DIR` | run logs and reports (default `import-logs`) |
| `--backfill-dir DIR` | where backfilled copies are written (default `csv-backfilled`) |
| `--in-place` | edit the exported CSVs instead of writing copies |
| `--no-backup` | skip the `.bak` copy taken before backfilling; only applies with `--in-place` |

## Which rows get imported

A row is imported when `REPORT TITLE` is filled in and both `GITHUB STATUS` and
`GITHUB LINK` are empty. Everything else is left alone and reported:

- **Already logged, or deliberately not logging** — any row with a link, or with
  *any* value in `GITHUB STATUS`. The tool never overwrites a status it did not
  write, so putting a status on a row is how you take it out of the import.

  Nothing is hardcoded here: `LOGGED`, `DUPLICATE`, `RETIRED`, `MISSING INFO`,
  `WONTFIX` and anything else a tester types all work the same way, and new
  values need no code change. Each distinct value is counted separately in the
  run summary, so a typo shows up as its own line rather than hiding.

  `RETIRED` is the intended way to shelve a bug that should never be logged:
  set it once and the row is skipped on every run, however complete its report.
- **Blocked** — the row wants importing but is not usable as written: an empty
  `BUG REPORT`, the sheet's own `Incomplete - video link missing!` sentinel, a
  spreadsheet error like `#REF!` left behind by a stale export, or a report
  title that is only its prefix (`GAME: GAME`, `WEB:`), which means the
  `REPORT TITLE` formula is pointing at the wrong cell. These stay blank and
  are picked up on a later run once the sheet is fixed.
- **Duplicates** — some rows are copied into several tabs, so a row repeating
  both the title *and* the body of one already seen is skipped. Body matters:
  testers legitimately file a run of bugs under one title (thirteen
  "Infoscanner word not appearing" rows, one per word), and those are distinct
  bugs that each get an issue. Rows that already carry a link count too, so a
  duplicate stays skipped on every later run rather than being logged once its
  twin is marked `LOGGED`.

Both the current tracker and the archived one work unchanged — the tool reads
the four columns by name, and tabs without them (notice pages, `HOW TO USE`)
are skipped with a warning rather than stopping the run.

## Safety

Running the tool twice does not create duplicate issues. The backfilled status
and link are what mark a row as done, so a second run simply finds nothing to do.

Every issue URL is appended to `import-logs/created.jsonl` the moment it is
created, *before* the CSV is written. If a run is interrupted between those two
steps, the next run reads that log, backfills the affected rows and creates
nothing new. `Ctrl+C` mid-run backfills whatever succeeded before exiting.

Failures are per-row: a row that GitHub rejects keeps its blank status and is
retried next run. Before backfilling, each CSV is copied to `<name>.csv.bak`.

`import-logs/report.csv` holds one line per tracker row — created, skipped,
blocked or failed, with the reason — so a row that quietly went nowhere is
visible rather than merely absent. A dry run writes it too.

## Seeing a backfill before you have a repo

```powershell
python make_sample_output.py                 # csv/ and csv-archived/
python make_sample_output.py csv-archived    # one tracker only
```

Copies the exported CSVs to `sample-output/` and runs a full import with the
GitHub CLI stubbed out, so you can see exactly what gets written back. Files
arrive as `<source-dir>--<tab>.csv`, so both trackers can be sampled in one run
and every row stays traceable to the tracker it came from.

Invented links are under `EXAMPLE-NOT-A-REAL-REPO` and can never be confused
with the genuine links already in the tracker. Never paste `sample-output/`
into the sheet.

## Tests

```powershell
python tests/test_end_to_end.py
```

Covers a first import, a no-op re-run, crash recovery from the resume log, and
dry-run isolation, with the GitHub CLI stubbed out — so it runs without touching
a real repository.

## Layout

| File | Role |
| --- | --- |
| `bugimport/cli.py` | argument parsing and the import loop |
| `bugimport/columns.py` | resolves the four columns by header name |
| `bugimport/select.py` | decides import / skip / blocked, dedupes |
| `bugimport/source_csv.py` | reads CSVs, writes the backfill |
| `bugimport/gh.py` | GitHub CLI wrapper |
| `export_tracker_csv.py` | `.xlsx` tracker to one CSV per tab |
| `make_sample_output.py` | sample run with fake URLs, for inspection only |

The Google Sheets source is a drop-in replacement for `source_csv.py`: the CLI
only calls `rows()`, `backfill()` and `describe()`, so adding it means writing
those three methods against `gspread` and choosing the source by argument.
