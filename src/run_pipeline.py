# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pandas==3.0.5",
#     "rdata==1.1.0",
#     "pyarrow==25.0.1",
# ]
# ///
"""Run the data-processing stage: uv run src/run_pipeline.py.

uv installs the declared dependencies in an isolated environment. With those
dependencies already installed, python src/run_pipeline.py also works.
Paths default to this script's project, independently of the working directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process and validate the three raw pg16 datasets.")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1],
        help="Project folder containing data/raw (defaults to this script's project).",
    )
    parser.add_argument(
        "--exposure-tolerance", type=float, default=1e-8,
        help="Absolute exposure comparison tolerance (default: 1e-8).",
    )
    args = parser.parse_args(argv)
    # Avoid creating __pycache__ files in the source package when running the CLI.
    sys.dont_write_bytecode = True
    try:
        from track_4.data_processing import DataValidationError, process_data
    except ModuleNotFoundError as exc:
        print(f"Missing dependency: {exc.name}. Run: uv run src/run_pipeline.py", file=sys.stderr)
        return 1
    try:
        result = process_data(args.project_root, args.exposure_tolerance)
    except DataValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name, frame in result.tables.items():
        print(f"Saved data/processed/{name}.parquet ({len(frame):,} rows, {len(frame.columns)} columns)")
    warnings = sum(check.status == "WARN" for check in result.checks)
    print(f"Data processing complete: {warnings} warnings, 0 failed checks.")
    print(f"Report: {args.project_root.resolve() / 'results' / 'data_processing.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
