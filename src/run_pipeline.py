# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pandas==3.0.5",
#     "rdata==1.1.0",
#     "pyarrow==25.0.1",
#     "numpy==2.5.3",
#     "scipy==1.18.1",
#     "matplotlib==3.11.1",
# ]
# ///
"""Run processing as needed, then simulation: uv run src/run_pipeline.py.

uv installs the declared dependencies in an isolated environment. With those
dependencies already installed, python src/run_pipeline.py also works.
Paths default to this script's project, independently of the working directory.
Existing cleaned files are reused. --stage processing explicitly refreshes them;
--stage simulation runs only simulation and requires existing cleaned inputs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the PG16 data and contract-simulation pipeline.")
    parser.add_argument("--stage", choices=("all", "processing", "simulation"), default="all",
                        help="Default: reuse existing cleaned data, process if missing, then simulate.")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1],
        help="Project folder containing data/raw (defaults to this script's project).",
    )
    parser.add_argument(
        "--exposure-tolerance", type=float, default=1e-8,
        help="Absolute exposure comparison tolerance (default: 1e-8).",
    )
    parser.add_argument("--years", type=int, help="Simulated years per grid point (default: spec, 500,000).")
    parser.add_argument("--vehicles", type=int, help="Annual fleet size (default: spec, 1,000).")
    parser.add_argument("--seed", type=int, default=20260907, help="Fixed Monte Carlo seed.")
    parser.add_argument("--batch-years", type=int, default=10000, help="Years per memory-bounded batch.")
    args = parser.parse_args(argv)
    # Avoid creating __pycache__ files in the source package when running the CLI.
    sys.dont_write_bytecode = True
    try:
        from track_4.data_processing import DataValidationError, process_data
    except ModuleNotFoundError as exc:
        print(f"Missing dependency: {exc.name}. Run: uv run src/run_pipeline.py", file=sys.stderr)
        return 1
    root = args.project_root.resolve()
    cleaned = [root / "data/processed" / f"clean_{name}.parquet"
               for name in ("train_policy", "train_claim", "test_policy")]
    if args.stage == "processing" or (args.stage == "all" and not all(path.is_file() for path in cleaned)):
        if args.stage == "all" and any(path.exists() for path in cleaned):
            print("Cleaned outputs are incomplete. Run --stage processing explicitly to refresh them.", file=sys.stderr)
            return 1
        try:
            result = process_data(root, args.exposure_tolerance)
        except DataValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for name, frame in result.tables.items():
            print(f"Saved data/processed/{name}.parquet ({len(frame):,} rows, {len(frame.columns)} columns)")
        warnings = sum(check.status == "WARN" for check in result.checks)
        print(f"Data processing complete: {warnings} warnings, 0 failed checks.")
        print(f"Report: {root / 'results' / 'data_processing.md'}")
    elif args.stage == "all":
        print("Using existing cleaned datasets.", flush=True)
    if args.stage != "processing":
        try:
            from track_4.simulation import SimulationError, SimulationSettings, read_specification, run_simulation
        except ModuleNotFoundError as exc:
            print(f"Missing dependency: {exc.name}. Run: uv run src/run_pipeline.py", file=sys.stderr)
            return 1
        try:
            spec = read_specification(root / "spec.qmd")
            settings = SimulationSettings(years=args.years if args.years is not None else spec["years"],
                                          vehicles=args.vehicles if args.vehicles is not None else spec["vehicles"],
                                          seed=args.seed, batch_years=args.batch_years)
            run_simulation(root, settings, progress=lambda message: print(message, flush=True))
        except (SimulationError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
