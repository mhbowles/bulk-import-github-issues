"""Command line entry point for the bulk issue importer."""

import argparse
import csv
import glob
import json
import sys
import time
from collections import Counter
from pathlib import Path

from . import gh
from .columns import MissingColumns
from .select import BLOCKED, IMPORT, SKIP, classify, dedupe
from .source_csv import CsvSource

DEFAULT_STATUS = "LOGGED"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bugimport",
        description="Create GitHub issues in bulk from Master Bug Tracker CSVs, "
                    "then backfill GITHUB STATUS and GITHUB LINK.",
    )
    parser.add_argument("csv", nargs="+", type=Path,
                        help="one CSV per tracker tab")
    parser.add_argument("--repo", metavar="OWNER/NAME",
                        help="target repository (required unless --dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="render every issue to the report and create nothing")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="create at most N issues this run")
    parser.add_argument("--label", action="append", default=[], metavar="NAME",
                        help="label to apply (repeatable); must already exist")
    parser.add_argument("--project", metavar="TITLE",
                        help="project to add issues to (needs the 'project' auth scope)")
    parser.add_argument("--project-status", metavar="TEXT",
                        help="value to set on each issue's project status field "
                             "(needs --project)")
    parser.add_argument("--project-field", default="Status", metavar="NAME",
                        help="single-select project field that --project-status "
                             "writes to (default: %(default)s)")
    parser.add_argument("--status", default=DEFAULT_STATUS, metavar="TEXT",
                        help="value written to GITHUB STATUS (default: %(default)s)")
    parser.add_argument("--sleep", type=float, default=1.0, metavar="SECONDS",
                        help="pause between creates, to stay under GitHub's "
                             "secondary rate limit (default: %(default)s)")
    parser.add_argument("--out-dir", type=Path, default=Path("import-logs"),
                        help="where run logs and reports are written "
                             "(default: %(default)s)")
    parser.add_argument("--backfill-dir", type=Path, default=Path("csv-backfilled"),
                        help="where backfilled copies of the CSVs are written "
                             "(default: %(default)s)")
    parser.add_argument("--in-place", action="store_true",
                        help="edit the exported CSVs instead of writing copies")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not write a .bak copy before backfilling; "
                             "only applies with --in-place")

    args = parser.parse_args(argv)
    if not args.dry_run and not args.repo:
        parser.error("--repo is required unless --dry-run is given")
    if args.project_status and not args.project:
        parser.error("--project-status needs --project to say which board")
    return args


def expand_inputs(paths):
    """Expand any wildcards the shell left alone.

    bash expands csv/*.csv before Python sees it; PowerShell passes it through
    verbatim to native commands. Expanding here means the same command works in
    both shells, and a pattern matching nothing is an error rather than a
    confusing "no such file" for a path containing a star.
    """
    expanded = []
    for path in paths:
        pattern = str(path)
        if not any(char in pattern for char in "*?["):
            expanded.append(path)
            continue
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise ValueError("no files match {}".format(pattern))
        expanded.extend(Path(match) for match in matches)
    return expanded


def load_sources(paths):
    """Load every usable CSV, reporting the rest rather than aborting.

    A glob over an exported tracker picks up notice and instruction tabs too.
    Those are skipped with a warning so one of them cannot stop the import.
    """
    paths = expand_inputs(paths)
    # Both trackers have a Game tab. Where file names collide, include the
    # parent directory in the label so every reported row still names exactly
    # one file - csv--Game.csv and csv-archived--Game.csv.
    duplicates = Counter(path.name for path in paths)

    sources, unusable = {}, []
    for path in paths:
        label = path.name
        if duplicates[label] > 1:
            label = "{}--{}".format(path.parent.name, label)
        try:
            source = CsvSource(path, label=label)
        except MissingColumns as error:
            unusable.append(str(error))
            continue
        if label in sources:
            raise ValueError(
                "two inputs are both labelled {} ({} and {}); rename one so "
                "rows can be traced back to a single file".format(
                    label, sources[label].path, source.path
                )
            )
        sources[label] = source
    return sources, unusable


def load_resume_log(path):
    """Return {(source, line, title): url} for issues created by an earlier run.

    Written before the sheet is touched, so a run killed between creating an
    issue and backfilling can be finished without opening duplicates.

    The title is part of the key because line numbers alone are not stable:
    a fresh export, or a different tracker with a same-named tab, puts other
    bugs on those lines, and matching them would backfill unrelated links.
    """
    done = {}
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            done[(entry["source"], entry["line"], entry["title"])] = entry["url"]
    return done


def write_dry_run(rows, path):
    with path.open("w", encoding="utf-8", newline="\n") as out:
        out.write("# Dry run\n\n")
        for row in rows:
            out.write("---\n\n## {}\n\n".format(row.title))
            out.write("_{} line {}_\n\n".format(row.source, row.line))
            out.write(row.body.rstrip() + "\n\n")


def write_backfill(source, rows, out_path, backup, attempts=20):
    """Write the backfilled copy, stepping aside if the name is unwritable.

    A spreadsheet holding the file open is the ordinary case, and it is the
    one moment in the run where the issue URLs exist only in the resume log.
    Falling back to "Game (1).csv" keeps them while leaving the locked file
    alone. Returns (rows written, the path actually used).

    Only copies step aside: with --in-place the alternate would land in the
    folder the CSVs are read from, where the next glob would import the same
    rows a second time.
    """
    if out_path is None:
        return source.backfill(rows, backup=backup), None
    try:
        return source.backfill(rows, backup=backup, out_path=out_path), out_path
    except OSError:
        for attempt in range(1, attempts + 1):
            alternate = out_path.with_name("{} ({}){}".format(
                out_path.stem, attempt, out_path.suffix))
            try:
                return (source.backfill(rows, backup=backup, out_path=alternate),
                        alternate)
            except OSError:
                continue
        raise


def write_report(decisions, outcomes, path):
    """Write one row per tracker row, whatever happened to it.

    Skipped and blocked rows go in alongside the created ones - a row that
    quietly went nowhere is exactly what someone reviewing a run needs to see.
    """
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["source", "line", "action", "title", "url", "detail"])
        for decision in decisions:
            row = decision.row
            action, url, detail = outcomes.get(
                (row.source, row.line), (decision.action, row.link, decision.reason)
            )
            writer.writerow([row.source, row.line, action, row.title, url, detail])


def reason_key(reason):
    """Strip the row-specific tail off a reason so like rows group together."""
    for separator in (" (", " - ", ": "):
        reason = reason.split(separator)[0]
    return reason


def group_by_reason(decisions, action):
    grouped = {}
    for decision in decisions:
        if decision.action == action:
            grouped.setdefault(reason_key(decision.reason), []).append(decision.row)
    return sorted(grouped.items(), key=lambda item: -len(item[1]))


def summarize(decisions, examples=4):
    counts = Counter(d.action for d in decisions)
    print("{} row(s) read: {} to import, {} skipped, {} blocked".format(
        len(decisions), counts[IMPORT], counts[SKIP], counts[BLOCKED]))

    blocked = group_by_reason(decisions, BLOCKED)
    if blocked:
        print("\nblocked (nothing was created for these):")
        for reason, rows in blocked:
            print("  {} x {}".format(len(rows), reason))
            for row in rows[:examples]:
                print("      {} line {}".format(row.source, row.line))
            if len(rows) > examples:
                print("      ... and {} more".format(len(rows) - examples))

    skipped = group_by_reason(decisions, SKIP)
    if skipped:
        print("\nskipped:")
        for reason, rows in skipped:
            print("  {} x {}".format(len(rows), reason))


def main(argv=None):
    args = parse_args(argv)

    try:
        sources, unusable = load_sources(args.csv)
    except (ValueError, OSError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2

    for message in unusable:
        print("skipping {}".format(message), file=sys.stderr)
    if not sources:
        print("error: none of the given files are bug tabs", file=sys.stderr)
        return 2

    decisions = dedupe([
        classify(row)
        for source in sources.values()
        for row in source.rows()
    ])
    summarize(decisions)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    pending = [d.row for d in decisions if d.action == IMPORT]

    # Applied before the dry run branches off, so the rendered report is
    # exactly what a real run with the same flags would create.
    if args.limit is not None:
        pending = pending[:args.limit]
        print("\nlimit: {} at most {} issue(s)".format(
            "rendering" if args.dry_run else "creating", args.limit))

    if args.dry_run:
        report = args.out_dir / "dry-run.md"
        write_dry_run(pending, report)
        write_report(decisions, {}, args.out_dir / "report.csv")
        print("\ndry run: {} issue(s) rendered to {}".format(len(pending), report))
        print("per-row outcome: {}".format(args.out_dir / "report.csv"))
        print("nothing was created and no CSV was modified")
        return 0

    if not pending:
        print("\nnothing to import")
        return 0

    resume_path = args.out_dir / "created.jsonl"
    already = load_resume_log(resume_path)
    if already:
        print("\nresuming: {} issue(s) were created by an earlier run and will "
              "only be backfilled".format(len(already)))

    try:
        gh.check_available()
        gh.check_repo(args.repo)
        if args.label:
            missing = set(args.label) - gh.existing_labels(args.repo)
            if missing:
                print("error: label(s) not defined on {}: {}".format(
                    args.repo, ", ".join(sorted(missing))), file=sys.stderr)
                return 2
        status_field = None
        if args.project_status:
            # The project may not be owned by the repo owner, but that is the
            # only sensible guess without another flag.
            status_field = gh.ProjectStatus(
                args.repo.split("/")[0], args.project,
                args.project_field, args.project_status,
            )
            print("\neach issue will be set to {}".format(status_field.describe()))
    except gh.GhError as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2

    print("\ncreating {} issue(s) on {}\n".format(len(pending), args.repo))

    updates = {}      # source name -> {line: (status, link)}
    outcomes = {}     # (source, line) -> (action, url, detail)
    failures = 0

    try:
        for position, row in enumerate(pending, 1):
            url = already.get((row.source, row.line, row.title))
            action = "created"
            if url is None:
                try:
                    # gh adds an issue to a project after creating it, so a
                    # failure there exits non-zero with the issue already
                    # open - and the retry would file it a second time. When
                    # the status is being set, apply() does the add itself,
                    # idempotently and after the fact, so leave it out here.
                    url = gh.create_issue(
                        args.repo, row.title, row.body, labels=args.label,
                        project=None if status_field else args.project,
                    )
                except gh.GhError as error:
                    failures += 1
                    print("  [{}/{}] FAILED {} line {}: {}".format(
                        position, len(pending), row.source, row.line, error))
                    outcomes[(row.source, row.line)] = ("failed", "", str(error))
                    continue

                # Recorded before the sheet is touched: if the run dies here,
                # the next one backfills instead of creating a second issue.
                with resume_path.open("a", encoding="utf-8", newline="\n") as log:
                    log.write(json.dumps({
                        "source": row.source, "line": row.line,
                        "title": row.title, "url": url,
                    }) + "\n")

                if args.sleep:
                    time.sleep(args.sleep)
            else:
                action = "backfilled"

            print("  [{}/{}] {} {}  <- {} line {}".format(
                position, len(pending),
                "created " if action == "created" else "resumed ",
                url, row.source, row.line))

            # The issue already exists, so a project failure is reported
            # against the row rather than treated as a failed import.
            detail = ""
            if status_field:
                try:
                    status_field.apply(url)
                except gh.GhError as error:
                    detail = "project status not set: {}".format(error)
                    print("      ! {}".format(detail))

            updates.setdefault(row.source, {})[row.line] = (args.status, url)
            outcomes[(row.source, row.line)] = (action, url, detail)
    except KeyboardInterrupt:
        print("\ninterrupted - backfilling the issues created so far")

    backfilled = 0
    unwritable = 0
    for name, rows in updates.items():
        # Labels are unique per run, so two trackers sharing a tab name
        # cannot overwrite each other here.
        out_path = None if args.in_place else args.backfill_dir / name
        try:
            if out_path is not None:
                args.backfill_dir.mkdir(parents=True, exist_ok=True)
            written, actual = write_backfill(
                sources[name], rows, out_path, not args.no_backup)
            backfilled += written
            note = "" if actual == out_path else "  ({} was not writable)".format(
                out_path)
            print("backfilled {} row(s) in {}{}".format(
                len(rows), actual or name, note))
        except OSError as error:
            # Usually the file is open in a spreadsheet. The issues are
            # already created, so losing the run over an unwritable copy
            # would throw away the report as well - report and carry on.
            unwritable += 1
            print("could not write {}: {}".format(out_path or name, error),
                  file=sys.stderr)

    report = args.out_dir / "report.csv"
    try:
        write_report(decisions, outcomes, report)
    except OSError as error:
        unwritable += 1
        print("could not write {}: {}".format(report, error), file=sys.stderr)

    created = sum(1 for action, _, _ in outcomes.values() if action == "created")
    print("\ncreated {} issue(s), backfilled {} row(s), {} failure(s)".format(
        created, backfilled, failures))
    print("report: {}".format(report))
    if updates and not args.in_place and not unwritable:
        print("the exported CSVs were not modified - paste the backfilled "
              "columns from {} into the tracker".format(args.backfill_dir))
    if failures:
        print("failed rows kept their blank status and will be retried next run")
    if unwritable:
        print("\n{} file(s) could not be written, usually because something has "
              "them open. Every issue that was created is recorded in {} - "
              "close the file and run the same command again to rebuild the "
              "outputs. Nothing will be created twice.".format(
                  unwritable, resume_path))
    return 1 if failures or unwritable else 0


if __name__ == "__main__":
    sys.exit(main())
