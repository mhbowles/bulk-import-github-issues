"""Header lookup for tracker CSVs.

The tracker tabs differ in their left-hand columns - some have PATH, some have
CATEGORY, Accessibility carries trailing blank columns - but every tab ends in
the same four, and those are the only ones this tool touches:

    REPORT TITLE   the issue title, already assembled by the sheet
    BUG REPORT     the issue body, already assembled by the sheet
    GITHUB STATUS  read to decide what to skip, backfilled after logging
    GITHUB LINK    read to decide what to skip, backfilled after logging

Formatting the report stays in the spreadsheet where QA can see and edit it;
this tool just carries it to GitHub. Columns are resolved by header name rather
than by position so a tab gaining or losing a column doesn't break the import.
"""

import re

# Internal key -> header names to look for, in priority order.
ALIASES = {
    "report_title": ["REPORT TITLE"],
    "bug_report": ["BUG REPORT"],
    "status": ["GITHUB STATUS"],
    "link": ["GITHUB LINK"],
}

REQUIRED = tuple(ALIASES)


def normalize(name):
    """Uppercase, trim, and collapse internal whitespace in a header cell."""
    return re.sub(r"\s+", " ", (name or "").strip().upper())


class MissingColumns(Exception):
    pass


class Columns:
    """Maps internal field keys to column indexes for one CSV's header row."""

    def __init__(self, header, source=""):
        self.header = header
        self.source = source
        normalized = [normalize(h) for h in header]

        self._index = {}
        for key, names in ALIASES.items():
            for name in names:
                if name in normalized:
                    self._index[key] = normalized.index(name)
                    break

        missing = [k for k in REQUIRED if k not in self._index]
        if missing:
            raise MissingColumns(
                "{}: missing required column(s): {}".format(
                    source or "csv", ", ".join(ALIASES[k][0] for k in missing)
                )
            )

    def index(self, key):
        return self._index[key]

    def get(self, row, key, default=""):
        """Read a field from a row, tolerating rows shorter than the header."""
        i = self._index[key]
        if i >= len(row):
            return default
        value = row[i]
        if isinstance(value, str):
            return value.strip()
        else:
            return default
