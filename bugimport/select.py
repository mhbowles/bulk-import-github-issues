"""Decide which tracker rows should become GitHub issues."""

import re
from collections import namedtuple

# Spreadsheet error values. If a CSV was exported while a formula was broken,
# these arrive as literal text and must never be posted to GitHub.
SPREADSHEET_ERRORS = re.compile(
    r"#(REF|N/A|VALUE|DIV/0|NAME|NUM|NULL|ERROR)[!?]?", re.IGNORECASE
)

# The sheet's own sentinel for a report it considers unfinished.
INCOMPLETE = "incomplete - video link missing!"

Row = namedtuple("Row", "source line title body status link")
Decision = namedtuple("Decision", "row action reason")

# action values
IMPORT = "import"
SKIP = "skip"
BLOCKED = "blocked"  # wants importing but the row is unusable as written


def read_row(cols, raw, source, line):
    return Row(
        source=source,
        line=line,
        title=cols.get(raw, "report_title"),
        body=cols.get(raw, "bug_report"),
        status=cols.get(raw, "status"),
        link=cols.get(raw, "link"),
    )


def looks_unfinished(title):
    """True when a report title carries its prefix but no actual bug title.

    REPORT TITLE is built as "<PATH or CATEGORY>: <TITLE>". If that formula
    points at the wrong cell - a cross-tab reference to a column that holds the
    prefix rather than the title - it renders as "GAME: GAME" or "GAME: ",
    which is a valid-looking string that would post a useless issue.
    """
    prefix, separator, rest = title.partition(":")
    if not separator:
        return False  # no prefix convention in use; nothing to compare against
    rest = rest.strip()
    return not rest or rest.lower() == prefix.strip().lower()


def classify(row):
    """Return a Decision for one row.

    A row is imported only when it has a rendered title and body and has not
    already been logged. Anything a human has already put in GITHUB STATUS -
    LOGGED, DUPLICATE, WONTFIX - is left alone; the tool never overwrites a
    status it did not write.
    """
    if not row.title:
        return Decision(row, SKIP, "no report title")

    if row.link:
        return Decision(row, SKIP, "already linked: {}".format(row.link))

    if row.status:
        return Decision(row, SKIP, "status already set to {}".format(row.status))

    for field, value in (("title", row.title), ("body", row.body)):
        error = SPREADSHEET_ERRORS.search(value)
        if error:
            return Decision(
                row,
                BLOCKED,
                "{} contains spreadsheet error {} - re-export the CSV once the "
                "sheet formulas resolve".format(field, error.group(0)),
            )

    if not row.body:
        return Decision(row, BLOCKED, "bug report is empty")

    # The sheet's own signal comes first: when QA has flagged a row as
    # unfinished, that is the more useful thing to report back.
    if row.body.strip().lower() == INCOMPLETE:
        return Decision(row, BLOCKED, "sheet marked the report incomplete (no video link)")

    if looks_unfinished(row.title):
        return Decision(
            row,
            BLOCKED,
            "report title is only its prefix ({!r}) - the REPORT TITLE formula "
            "is not picking up this row's bug title".format(row.title),
        )

    return Decision(row, IMPORT, "")


def _fingerprint(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def dedupe(decisions):
    """Collapse rows that repeat the same report across tabs.

    Some rows are copied into several tabs, so an unfiltered run would open the
    same issue many times. The key is the title *and* the body: testers
    legitimately file a run of bugs under one title - thirteen "Infoscanner
    word not appearing" rows, one per word - and those are distinct bugs that
    must each get an issue. Only a row identical in both is a true duplicate.

    First occurrence wins; later ones are skipped and reported so nothing
    disappears silently.
    """
    seen = {}

    # Rows that already carry a link are authoritative, so seed them first.
    # Without this, marking one twin LOGGED would leave the other looking
    # unique on the next run, and it would be logged a second time.
    for decision in decisions:
        if decision.row.link:
            key = (_fingerprint(decision.row.title), _fingerprint(decision.row.body))
            seen.setdefault(key, decision.row)

    result = []
    for decision in decisions:
        if decision.action != IMPORT:
            result.append(decision)
            continue
        key = (_fingerprint(decision.row.title), _fingerprint(decision.row.body))
        if key in seen:
            first = seen[key]
            where = "{} line {}".format(first.source, first.line)
            result.append(
                Decision(
                    decision.row,
                    SKIP,
                    "duplicate of {}{}".format(
                        where, " ({})".format(first.link) if first.link else ""
                    ),
                )
            )
            continue
        seen[key] = decision.row
        result.append(decision)
    return result
