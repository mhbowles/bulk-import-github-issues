"""CSV-backed tracker source.

One CSV per tab, exported from the Master Bug Tracker (File > Download >
Comma-separated values, with the tab selected). Row numbers reported by this
module are file line numbers, which match the row numbers in the sheet because
the header occupies line 1.

The Google Sheets source planned for later implements the same three methods -
`rows`, `backfill`, `describe` - so the CLI does not need to know which is in
use.
"""

import csv
import shutil
from pathlib import Path

from .columns import Columns
from .select import read_row

# Tracker cells hold long descriptions and console dumps; the default limit
# rejects fields that are merely large rather than malformed.
csv.field_size_limit(10 * 1024 * 1024)


class CsvSource:
    def __init__(self, path, label=None):
        self.path = Path(path)
        # How this file is named in reports. Defaults to the file name; the
        # caller supplies something longer when two inputs would collide.
        self.label = label or self.path.name
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            self._rows = list(csv.reader(handle))
        if not self._rows:
            raise ValueError("{}: file is empty".format(self.path))
        self.columns = Columns(self._rows[0], source=self.label)

    def describe(self):
        return self.label

    def rows(self):
        """Yield a Row per data line, in file order."""
        for offset, raw in enumerate(self._rows[1:]):
            yield read_row(self.columns, raw, self.describe(), offset + 2)

    def backfill(self, updates, backup=True, out_path=None):
        """Write status and link back to the given lines.

        `updates` maps line number -> (status, link). The rest of every row is
        written back byte-for-byte as it was read, so unrelated columns and
        their formatting survive the round trip.

        With `out_path`, the updated copy is written there and the exported CSV
        is left exactly as it was - no backup is taken, because nothing the
        backup would protect is being changed.
        """
        if not updates:
            return 0

        status_col = self.columns.index("status")
        link_col = self.columns.index("link")
        width = len(self._rows[0])

        written = 0
        for line, (status, link) in updates.items():
            raw = self._rows[line - 1]
            if len(raw) < width:
                raw.extend([""] * (width - len(raw)))
            raw[status_col] = status
            raw[link_col] = link
            written += 1

        if out_path is None and backup:
            backup_path = self.path.with_suffix(self.path.suffix + ".bak")
            if not backup_path.exists():
                shutil.copy2(self.path, backup_path)

        target = Path(out_path) if out_path else self.path
        with target.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(self._rows)

        return written
