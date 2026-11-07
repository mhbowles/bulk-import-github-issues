"""End-to-end tests with the GitHub CLI stubbed out.

Run directly - no pytest needed:

    python tests/test_end_to_end.py
"""

import csv
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bugimport import gh
from bugimport.cli import main

HEADER = ["TESTER", "TITLE", "GITHUB STATUS", "GITHUB LINK",
          "REPORT TITLE", "BUG REPORT"]

ROWS = [
    # importable
    ["Milo", "Tooltip typo", "", "", "WEB: Tooltip typo", "### Priority:\nP3"],
    # already logged, must be left alone
    ["Liv", "Old bug", "LOGGED", "https://github.com/a/b/issues/1",
     "WEB: Old bug", "### Priority:\nP2"],
    # blocked by the sheet's own sentinel
    ["Liv", "No video", "", "", "WEB: No video",
     "Incomplete - video link missing!"],
    # blocked by a broken formula
    ["Liv", "Broken", "", "", "#REF!", "#REF!"],
    # importable
    ["Milo", "Missing art", "", "", "GAME: Missing art", "### Priority:\nP1"],
    # blocked: REPORT TITLE formula picked up the prefix instead of the title
    ["Liv", "Prefix only", "", "", "GAME: GAME", "### Priority:\nP1"],
    # same title, different bodies: distinct bugs, both must be logged
    ["Liv", "Infoscanner", "", "", "GAME: Infoscanner", "the word 'biased'"],
    ["Liv", "Infoscanner", "", "", "GAME: Infoscanner", "the word 'algorithms'"],
    # identical to the "Missing art" row above: a real duplicate
    ["Liv", "Missing art", "", "", "GAME: Missing art", "### Priority:\nP1"],
    # RETIRED: a complete, unlogged report that must never reach GitHub
    ["Liv", "Retired bug", "RETIRED", "", "GAME: Retired bug",
     "### Priority:\nP1\n\n### Description of Bug:\nold, not worth logging"],
]

calls = []
projects_at_create = []


def fake_create_issue(repo, title, body, labels=(), project=None, **kwargs):
    calls.append(title)
    projects_at_create.append(project)
    return "https://github.com/{}/issues/{}".format(repo, 100 + len(calls))


class FakeProjectStatus:
    """Stands in for gh.ProjectStatus without touching the network."""

    applied = []

    def __init__(self, owner, project, field, value):
        self.owner, self.project, self.field, self.value = (
            owner, project, field, value)

    def describe(self):
        return "{} = {!r} on project {!r}".format(
            self.field, self.value, self.project)

    def apply(self, issue_url):
        FakeProjectStatus.applied.append((issue_url, self.field, self.value))


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def write_fixture(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  ok: {}".format(message))


def run(work):
    csv_path = work / "Game.csv"
    out_dir = work / "logs"
    filled_dir = work / "backfilled"
    filled = filled_dir / "Game.csv"
    argv = [str(csv_path), "--repo", "alterea/aoi", "--out-dir", str(out_dir),
            "--backfill-dir", str(filled_dir), "--sleep", "0"]

    print("\n[1] first run creates only the importable rows")
    write_fixture(csv_path, [row[:] for row in ROWS])
    code = main(argv)
    check(code == 0, "exit code 0")
    check(calls == ["WEB: Tooltip typo", "GAME: Missing art",
                    "GAME: Infoscanner", "GAME: Infoscanner"],
          "created 4 issues, skipping logged/blocked rows (got {})".format(calls))
    check(calls.count("GAME: Infoscanner") == 2,
          "same title with different bodies logs both bugs")
    check(calls.count("GAME: Missing art") == 1,
          "a row identical in title and body is deduped")

    check(read_csv(csv_path)[1][2] == "",
          "the exported CSV is never modified")
    check(not (work / "Game.csv.bak").exists(),
          "no .bak needed when the input is left alone")

    rows = read_csv(filled)
    check(rows[1][2] == "LOGGED" and rows[1][3] == "https://github.com/alterea/aoi/issues/101",
          "row 2 backfilled with status and link")
    check(rows[5][2] == "LOGGED" and rows[5][3] == "https://github.com/alterea/aoi/issues/102",
          "row 6 backfilled with status and link")
    check(rows[2][3] == "https://github.com/a/b/issues/1",
          "already-logged row untouched")
    check(rows[3][2] == "" and rows[4][2] == "" and rows[6][2] == "",
          "blocked rows left blank for a later run")
    check("GAME: Retired bug" not in calls,
          "RETIRED row never reaches GitHub")
    check(rows[10][2] == "RETIRED" and rows[10][3] == "",
          "RETIRED status is preserved, not overwritten")
    check("GAME: GAME" not in calls,
          "prefix-only report title never reaches GitHub")
    check(rows[0] == HEADER and rows[1][0] == "Milo",
          "unrelated columns survive the round trip")
    check(read_csv(filled)[0] == HEADER and len(read_csv(filled)) == len(ROWS) + 1,
          "the copy is a whole CSV, not just the changed rows")

    log = [json.loads(line) for line in
           (out_dir / "created.jsonl").read_text(encoding="utf-8").splitlines()]
    check(len(log) == 4, "resume log has 4 entries")

    print("\n[2] second run is a no-op (idempotent)")
    del calls[:]
    code = main(argv)
    check(code == 0 and calls == [], "nothing created on re-run")

    print("\n[3] crash recovery: sheet lost the backfill, resume log survived")
    del calls[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    code = main(argv)
    check(calls == [], "no duplicate issues created")
    check(read_csv(filled)[1][3] == "https://github.com/alterea/aoi/issues/101",
          "backfilled from the resume log instead")

    print("\n[4] every created issue gets the project status")
    del calls[:]
    del projects_at_create[:]
    del FakeProjectStatus.applied[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    shutil.rmtree(out_dir)
    code = main(argv + ["--project", "Bugs", "--project-status", "To Be Organized"])
    check(code == 0, "exit code 0")
    check(len(FakeProjectStatus.applied) == len(calls) == 4,
          "status applied to all 4 new issues (got {})".format(
              FakeProjectStatus.applied))
    check(all(value == "To Be Organized" and field == "Status"
              for _, field, value in FakeProjectStatus.applied),
          "status field and value passed through")
    check(set(projects_at_create) == {None},
          "the board is joined by apply(), never by a retryable create")

    print("\n[5] --project-status without --project is rejected")
    del calls[:]
    try:
        main([str(csv_path), "--repo", "alterea/aoi",
              "--project-status", "To Be Organized"])
        check(False, "should have exited")
    except SystemExit as error:
        check(error.code == 2 and calls == [], "exits 2 and creates nothing")

    print("\n[6] dry run creates nothing and writes nothing")
    del calls[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    shutil.rmtree(out_dir)
    code = main([str(csv_path), "--dry-run", "--out-dir", str(out_dir)])
    check(code == 0 and calls == [], "no issues created")
    check(read_csv(csv_path)[1][2] == "", "csv untouched")
    check(not filled_dir.exists() or read_csv(filled)[1][2] == "LOGGED",
          "a dry run writes no backfilled copy of its own")
    body = (out_dir / "dry-run.md").read_text(encoding="utf-8")
    check("WEB: Tooltip typo" in body and "No video" not in body,
          "dry-run report lists only importable rows")

    print("\n[7] --limit applies to dry runs too")
    del calls[:]
    shutil.rmtree(out_dir)
    code = main([str(csv_path), "--dry-run", "--out-dir", str(out_dir), "--limit", "1"])
    body = (out_dir / "dry-run.md").read_text(encoding="utf-8")
    check(code == 0 and calls == [], "no issues created")
    check(body.count("\n## ") == 1 and "WEB: Tooltip typo" in body,
          "only the first importable row is rendered")

    print("\n[8] --in-place edits the export, with a backup")
    del calls[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    shutil.rmtree(out_dir)
    code = main(argv + ["--in-place"])
    check(code == 0 and len(calls) == 4, "created 4 issues")
    check(read_csv(csv_path)[1][2] == "LOGGED", "the export itself is edited")
    check((work / "Game.csv.bak").exists(), "backup written")
    check(read_csv(work / "Game.csv.bak")[1][2] == "",
          "the backup holds the pre-run state")

    print("\n[9] an unwritable backfill copy does not lose the run")
    del calls[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    shutil.rmtree(out_dir)
    blocked = work / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    code = main([str(csv_path), "--repo", "alterea/aoi", "--out-dir", str(out_dir),
                 "--backfill-dir", str(blocked), "--sleep", "0"])
    check(len(calls) == 4, "the issues were still created")
    check(code == 1, "exit code 1 flags the unwritten file")
    check(len((out_dir / "created.jsonl").read_text(encoding="utf-8").splitlines()) == 4,
          "every created URL is in the resume log")
    check((out_dir / "report.csv").exists(), "the report is still written")

    print("\n[10] a locked copy steps aside to Game (1).csv")
    del calls[:]
    write_fixture(csv_path, [row[:] for row in ROWS])
    shutil.rmtree(out_dir)
    shutil.rmtree(filled_dir, ignore_errors=True)
    filled_dir.mkdir(parents=True)
    filled.write_text("held open by a spreadsheet", encoding="utf-8")
    os.chmod(filled, stat.S_IREAD)      # the closest portable stand-in for a lock
    try:
        code = main(argv)
        stepped = filled_dir / "Game (1).csv"
        check(code == 0, "exit code 0 - stepping aside is not a failure")
        check(stepped.exists(), "wrote the numbered alternate")
        check(read_csv(stepped)[1][2] == "LOGGED", "the alternate holds the backfill")
        check(filled.read_text(encoding="utf-8") == "held open by a spreadsheet",
              "the locked file is left untouched")
    finally:
        os.chmod(filled, stat.S_IWRITE)


if __name__ == "__main__":
    gh.create_issue = fake_create_issue
    gh.check_available = lambda: None
    gh.check_repo = lambda repo: None
    gh.existing_labels = lambda repo: {"bug"}
    gh.ProjectStatus = FakeProjectStatus

    work = Path(tempfile.mkdtemp(prefix="bugimport-test-"))
    try:
        run(work)
        print("\nall checks passed")
    finally:
        shutil.rmtree(work, ignore_errors=True)
