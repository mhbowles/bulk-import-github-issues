"""Export each tab of an .xlsx tracker to its own CSV.

Day to day you export CSVs straight from Google Sheets (File > Download >
Comma-separated values, once per tab). This script covers the other case:
turning an archived .xlsx copy of the tracker into the same set of CSVs, which
is how the importer gets tested against previously logged bugs.

    python export_tracker_csv.py "Master Bug Tracker.xlsx" --out-dir csv
"""

import argparse
import csv
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is required: pip install -r requirements.txt")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bugimport.columns import Columns, MissingColumns


def safe_name(title):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-") or "sheet"


def cell_text(value):
    """Render a cell the way a Google Sheets CSV export would."""
    if value is None:
        return ""
    text = str(value)
    # openpyxl hands back datetimes; the tracker only ever holds dates.
    if text.endswith(" 00:00:00"):
        text = text[: -len(" 00:00:00")]
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("csv"))
    parser.add_argument("--prefix", default="", metavar="NAME",
                        help="prepend NAME to each file, e.g. --prefix archived "
                             "gives archived--Game.csv. Use it when exporting "
                             "two trackers that share tab names, so rows stay "
                             "traceable to the tracker they came from")
    args = parser.parse_args(argv)

    workbook = openpyxl.load_workbook(args.workbook, data_only=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for sheet in workbook.worksheets:
        rows = [[cell_text(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)]
        # Trim the trailing all-blank rows Sheets leaves behind.
        while rows and not any(cell for cell in rows[-1]):
            rows.pop()

        # Trackers carry notice and instruction tabs alongside the bug tabs.
        # Anything without the four columns the importer needs is not a bug tab.
        try:
            Columns(rows[0] if rows else [], source=sheet.title)
        except MissingColumns:
            print("{:<24} skipped (not a bug tab)".format(sheet.title))
            continue

        name = safe_name(sheet.title)
        if args.prefix:
            name = "{}--{}".format(safe_name(args.prefix), name)
        path = args.out_dir / "{}.csv".format(name)
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
        print("{:<24} {:>4} row(s) -> {}".format(sheet.title, len(rows) - 1, path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
