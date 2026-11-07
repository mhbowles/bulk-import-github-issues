"""Produce a sample import run without touching GitHub.

Copies exported CSVs to sample-output/, runs the importer against them with the
GitHub CLI stubbed out, and backfills fake issue URLs. Use it to see what a real
run would write back before you have a repo to point at.

    python make_sample_output.py                    # csv/ and csv-archived/
    python make_sample_output.py csv-archived       # just the archived tracker

Files are copied in as <source-dir>--<tab>.csv, so two trackers that share tab
names can be sampled in one run and every row stays traceable to the tracker it
came from.

Every URL it invents is under FAKE_REPO, which is not a real repository, so a
sample row can never be mistaken for a logged one:

    https://github.com/EXAMPLE-NOT-A-REAL-REPO/sample/issues/4001

Genuine links already in the tracker point at the real project instead.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bugimport import gh
from bugimport.cli import main as run_import

FAKE_REPO = "EXAMPLE-NOT-A-REAL-REPO/sample"
DEFAULT_SOURCES = ["csv", "csv-archived"]
OUT_DIR = ROOT / "sample-output"


def collect(source_dirs):
    """Copy every CSV into OUT_DIR under a name prefixed with its directory."""
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    copied = []
    for source in source_dirs:
        directory = ROOT / source
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.csv")):
            target = OUT_DIR / "{}--{}".format(directory.name, path.name)
            shutil.copy2(path, target)
            copied.append(target)
        print("{:<16} {:>3} file(s)".format(directory.name, len(list(directory.glob('*.csv')))))
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="*", default=DEFAULT_SOURCES,
                        help="directories of exported CSVs (default: {})".format(
                            " ".join(DEFAULT_SOURCES)))
    args = parser.parse_args(argv)

    csvs = collect(args.sources or DEFAULT_SOURCES)
    if not csvs:
        sys.exit("no CSVs found - run export_tracker_csv.py first")

    created = [0]

    def fake_create_issue(repo, title, body, labels=(), project=None, **kwargs):
        created[0] += 1
        return "https://github.com/{}/issues/{}".format(FAKE_REPO, 4000 + created[0])

    gh.create_issue = fake_create_issue
    gh.check_available = lambda: None
    gh.check_repo = lambda repo: None
    gh.existing_labels = lambda repo: set()

    print()
    code = run_import([str(path) for path in csvs]
                      + ["--repo", FAKE_REPO, "--sleep", "0", "--no-backup",
                         "--out-dir", str(OUT_DIR / "import-logs")])

    print("\nSample only. Every link containing EXAMPLE-NOT-A-REAL-REPO was")
    print("invented by this script; any other link was already in the tracker.")
    return code


if __name__ == "__main__":
    sys.exit(main())
