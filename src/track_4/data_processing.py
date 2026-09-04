"""Load, type, validate, and save the three separate Pricing Game 2016 tables.

Schema reference: https://dutangc.github.io/CASdatasets/reference/pricingame.html
No rows are joined, concatenated, dropped, deduplicated, or imputed. Warnings
preserve the data; structural/type errors and broken claim checks prevent export.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from uuid import uuid4

import pandas as pd
import rdata


DOCUMENTATION_URL = "https://dutangc.github.io/CASdatasets/reference/pricingame.html"
DEFAULT_EXPOSURE_TOLERANCE = 1e-8
POLICY_FEATURES = (
    "Year", "BeginDate", "EndDate", "PolicyAgeCateg", "CompanyCreation",
    "FleetMgt", "Area", "FleetSizeCateg", "PayFreq", "Exposure", "VehiclAge",
    "Deduc", "VehiclNb", "SumInsured", "PolicyCateg", "VehiclCateg", "PolicyID",
    "BusinessType", "ChannelDist", "VehiclPower", "LicNb",
)
CLAIM_COLUMNS = (
    "BeginDate", "Year", "EndDate", "DirectComp", "CompRate", "SettlYear",
    "ClaimCharge", "PolicyID", "LicNb",
)
REQUIRED_COLUMNS = {
    "pg16trainpol": (*POLICY_FEATURES, "ClaimNb"),
    "pg16trainclaim": CLAIM_COLUMNS,
    "pg16test": POLICY_FEATURES,
}
# VehiclPower is stored as anonymized P1...P11 labels, not measured power.
CATEGORY_COLUMNS = (
    "PolicyAgeCateg", "FleetMgt", "Area", "FleetSizeCateg", "PayFreq",
    "VehiclAge", "Deduc", "SumInsured", "PolicyCateg", "VehiclCateg",
    "BusinessType", "ChannelDist", "VehiclPower",
)
INTEGER_COLUMNS = ("Year", "VehiclNb", "ClaimNb", "CompRate", "SettlYear")
FLOAT_COLUMNS = ("Exposure", "ClaimCharge")
EXPOSURE_COLUMNS = ("ExposureFromDates", "ExposureDifference", "ExposureMismatch")
OUTPUT_NAMES = {
    "pg16trainpol": "clean_train_policy",
    "pg16trainclaim": "clean_train_claim",
    "pg16test": "clean_test_policy",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass
class ProcessingResult:
    """Clean tables, keyed by output name, and all validation findings."""

    tables: dict[str, pd.DataFrame]
    checks: list[Check]

    @property
    def has_errors(self) -> bool:
        return any(check.status == "FAIL" for check in self.checks)


class DataValidationError(ValueError):
    """Input data cannot be safely exported; see the processing report."""


def _r_date_values(values, attributes):
    """Keep R Date day offsets for explicit conversion during validation."""
    return values


def load_raw_data(raw_dir: Path | str) -> dict[str, pd.DataFrame]:
    """Load each named R object from its own .rda file without requiring R."""
    tables = {}
    constructors = dict(rdata.conversion.DEFAULT_CLASS_MAP)
    constructors["Date"] = _r_date_values
    for name in REQUIRED_COLUMNS:
        path = Path(raw_dir) / f"{name}.rda"
        objects = rdata.read_rda(path, constructor_dict=constructors)
        if name not in objects or not isinstance(objects[name], pd.DataFrame):
            raise DataValidationError(f"{path.name} must contain a dataframe named {name}.")
        tables[name] = objects[name].copy().reset_index(drop=True)
    return tables


def _identifier(values: pd.Series) -> pd.Series:
    # Numeric R identifiers should become '123', not '123.0'. Text IDs retain
    # their original spelling, including leading zeroes.
    if pd.api.types.is_numeric_dtype(values.dtype):
        values = values.astype("Int64")
    return values.astype(pd.StringDtype(storage="pyarrow"))


def _date(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values.dtype):
        # R Date values count days since 1970-01-01, not nanoseconds.
        parsed = pd.to_datetime(
            values.astype("float64"), unit="D", origin="unix", errors="raise"
        )
    else:
        parsed = pd.to_datetime(values, format="ISO8601", errors="raise")
    return parsed.astype("datetime64[ns]")


def _binary(values: pd.Series) -> pd.Series:
    labels = values.astype("string").str.strip().str.lower()
    mapping = {
        "yes": True, "no": False, "true": True, "false": False,
        "1": True, "0": False, "1.0": True, "0.0": False,
    }
    invalid = labels.notna() & ~labels.isin(mapping)
    if invalid.any():
        raise ValueError(f"unrecognized binary values: {labels[invalid].unique().tolist()[:5]}")
    return labels.map(mapping).astype("boolean")


def _numeric(values: pd.Series, dtype: str) -> pd.Series:
    parsed = pd.to_numeric(values, errors="raise")
    if parsed.isin([float("inf"), float("-inf")]).any():
        raise ValueError("infinite numeric values")
    return parsed.astype(dtype)


def _convert_table(name: str, frame: pd.DataFrame, checks: list[Check]) -> None:
    converters = {
        "PolicyID": _identifier,
        "LicNb": _identifier,
        "BeginDate": _date,
        "EndDate": _date,
        "CompanyCreation": _binary,
        "DirectComp": _binary,
        **{column: lambda values: _numeric(values, "Int64") for column in INTEGER_COLUMNS},
        **{column: lambda values: _numeric(values, "Float64") for column in FLOAT_COLUMNS},
    }
    errors = []
    for column, convert in converters.items():
        if column not in frame:
            continue
        try:
            converted = convert(frame[column])
            if (frame[column].notna() & converted.isna()).any():
                raise ValueError("conversion would turn nonmissing values into missing values")
            frame[column] = converted
        except (ValueError, TypeError, OverflowError) as exc:
            errors.append(f"{column}: {exc}")
    checks.append(Check(
        f"{name}: types", "FAIL" if errors else "PASS",
        "; ".join(errors) if errors else "Identifiers, dates, numeric fields, and binary fields converted without new missing values.",
    ))


def _validate_policy(name: str, frame: pd.DataFrame, checks: list[Check], tolerance: float) -> None:
    identifiers = frame["PolicyID"]
    missing_ids = identifiers.isna() | identifiers.str.strip().eq("").fillna(False)
    checks.append(Check(
        f"{name}: PolicyID present", "FAIL" if missing_ids.any() else "PASS",
        f"{int(missing_ids.sum()):,} missing or blank identifiers.",
    ))
    key = ["PolicyID", "LicNb", "Year", "BeginDate", "EndDate"]
    repeated_keys = int(frame.duplicated(key).sum())
    checks.append(Check(
        f"{name}: policy combination uniqueness", "WARN" if repeated_keys else "PASS",
        f"{repeated_keys:,} duplicate occurrences of (PolicyID, LicNb, Year, BeginDate, EndDate).",
    ))
    bad_ranges = (frame["EndDate"] < frame["BeginDate"]).fillna(False)
    bad_values = ((frame["Exposure"] < 0) | (frame["VehiclNb"] < 1)).fillna(False)
    checks.append(Check(
        f"{name}: dates and ranges", "FAIL" if bad_ranges.any() or bad_values.any() else "PASS",
        f"{int(bad_ranges.sum()):,} reversed periods; {int(bad_values.sum()):,} rows with negative exposure or vehicle count below one.",
    ))
    expected = ((frame["EndDate"] - frame["BeginDate"]).dt.total_seconds() / (86400 * 365)).astype("Float64")
    difference = frame["Exposure"] - expected
    comparable = difference.notna()
    mismatch = (difference.abs() > tolerance).astype("boolean")
    frame["ExposureFromDates"] = expected
    frame["ExposureDifference"] = difference
    frame["ExposureMismatch"] = mismatch
    count = int(mismatch.sum())
    larger = int((difference.abs() > 0.005 + tolerance).sum())
    maximum = difference.abs().max()
    max_text = "n/a" if pd.isna(maximum) else f"{maximum:.9f}"
    checks.append(Check(
        f"{name}: Exposure", "WARN" if count or not comparable.all() else "PASS",
        f"{count:,}/{int(comparable.sum()):,} comparable rows differ; "
        f"{int((~comparable).sum()):,} not checkable; {larger:,} exceed 0.005 + tolerance; "
        f"max absolute difference {max_text}. Supplied Exposure retained.",
    ))


def clean_data(
    raw_tables: dict[str, pd.DataFrame],
    exposure_tolerance: float = DEFAULT_EXPOSURE_TOLERANCE,
) -> ProcessingResult:
    """Return separate typed tables and findings without modifying inputs.

    WARN findings are retained for review. A result containing FAIL findings
    must not be used as clean output; process_data enforces that rule on export.
    ExposureMismatch is nullable when dates or exposure are missing.
    """
    if not isfinite(exposure_tolerance) or exposure_tolerance < 0:
        raise ValueError("Exposure tolerance must be finite and nonnegative.")
    tables = {}
    checks = []
    for name, required in REQUIRED_COLUMNS.items():
        if name not in raw_tables:
            raise DataValidationError(f"Missing dataset: {name}")
        frame = raw_tables[name].copy().reset_index(drop=True)
        tables[name] = frame
        missing = sorted(set(required) - set(frame.columns))
        extra = sorted(set(frame.columns) - set(required))
        duplicate_columns = frame.columns[frame.columns.duplicated()].tolist()
        reserved = sorted(set(EXPOSURE_COLUMNS) & set(frame.columns))
        schema_error = bool(missing or duplicate_columns or reserved or frame.empty)
        checks.append(Check(
            f"{name}: columns", "FAIL" if schema_error else "WARN" if extra else "PASS",
            f"{len(frame):,} rows, {len(frame.columns)} columns; "
            f"missing: {missing or 'none'}; unexpected: {extra or 'none'}"
            + (f"; duplicate columns: {duplicate_columns}" if duplicate_columns else "")
            + (f"; reserved diagnostic columns already present: {reserved}" if reserved else "")
            + ("; dataset is empty" if frame.empty else "") + ".",
        ))
    outputs = {OUTPUT_NAMES[name]: frame for name, frame in tables.items()}
    result = ProcessingResult(outputs, checks)
    if result.has_errors:
        return result

    for name, frame in tables.items():
        _convert_table(name, frame, checks)
        missing = frame.isna().sum()
        details = ", ".join(f"{column}={int(count):,}" for column, count in missing.items() if count)
        checks.append(Check(
            f"{name}: missing values", "WARN" if missing.sum() else "PASS",
            f"{int(missing.sum()):,} missing cells" + (f" ({details}); retained without imputation." if details else " across all columns."),
        ))
    if result.has_errors:
        return result

    train, claims, test = (tables[name] for name in REQUIRED_COLUMNS)
    train_features = [column for column in train if column != "ClaimNb"]
    same_features = set(train_features) == set(test.columns)
    checks.append(Check(
        "Train/test feature columns", "PASS" if same_features else "FAIL",
        f"Train-only: {sorted(set(train_features) - set(test.columns)) or 'none'}; "
        f"test-only: {sorted(set(test.columns) - set(train_features)) or 'none'}. ClaimNb is a training outcome.",
    ))
    category_differences = []
    for column in CATEGORY_COLUMNS:
        train_values, test_values = train[column].astype("string"), test[column].astype("string")
        train_levels, test_levels = set(train_values.dropna()), set(test_values.dropna())
        if train_levels != test_levels:
            category_differences.append(
                f"{column}: train-only {sorted(train_levels - test_levels) or 'none'}, "
                f"test-only {sorted(test_levels - train_levels) or 'none'}"
            )
        # Sorting stabilizes storage; ordered=False explicitly carries no rank.
        dtype = pd.CategoricalDtype(sorted(train_levels | test_levels), ordered=False)
        train[column], test[column] = train_values.astype(dtype), test_values.astype(dtype)
    checks.append(Check(
        "Categorical levels", "WARN" if category_differences else "PASS",
        "; ".join(category_differences) if category_differences else "All observed categorical levels occur in both policy tables.",
    ))
    dtype_differences = [column for column in train_features if column in test and train[column].dtype != test[column].dtype]
    checks.append(Check(
        "Train/test feature types", "FAIL" if dtype_differences else "PASS",
        f"Mismatched types: {dtype_differences or 'none'}; shared unordered category definitions.",
    ))
    for name in ("pg16trainpol", "pg16test"):
        _validate_policy(name, tables[name], checks, exposure_tolerance)

    orphan = claims["PolicyID"].isna() | ~claims["PolicyID"].isin(train["PolicyID"].dropna())
    checks.append(Check(
        "Claim-to-policy relationship", "FAIL" if orphan.any() else "PASS",
        f"{int(orphan.sum()):,} claim rows have a missing or unknown PolicyID.",
    ))
    counts_valid = train["ClaimNb"].notna().all() and (train["ClaimNb"] >= 0).all()
    total = int(train["ClaimNb"].sum()) if counts_valid else None
    checks.append(Check(
        "Claim count reconciliation", "PASS" if total == len(claims) else "FAIL",
        f"Claim rows = {len(claims):,}; sum(training ClaimNb) = {total:,}." if total is not None
        else "ClaimNb contains missing or negative values; a complete total cannot be reconciled.",
    ))
    if counts_valid and not orphan.any():
        expected_counts = train.groupby("PolicyID")["ClaimNb"].sum()
        observed_counts = claims.groupby("PolicyID").size().reindex(expected_counts.index, fill_value=0)
        differences = int((expected_counts != observed_counts).sum())
        checks.append(Check(
            "Claim counts per PolicyID", "FAIL" if differences else "PASS",
            f"{differences:,} IDs disagree after aggregating counts for validation only.",
        ))
    negative, zero = int((claims["ClaimCharge"] < 0).sum()), int((claims["ClaimCharge"] == 0).sum())
    checks.append(Check(
        "ClaimCharge values", "WARN" if negative or zero else "PASS",
        f"{negative:,} negative and {zero:,} zero charges; retained without adjustment.",
    ))

    # Align feature order while retaining any unexpected columns for review.
    for name, frame in tables.items():
        original_columns = list(REQUIRED_COLUMNS[name])
        remainder = [column for column in frame if column not in original_columns]
        outputs[OUTPUT_NAMES[name]] = frame.loc[:, original_columns + remainder]
    return result


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _write_report(path: Path, result: ProcessingResult, tolerance: float, exported: bool) -> None:
    warnings = sum(check.status == "WARN" for check in result.checks)
    failures = sum(check.status == "FAIL" for check in result.checks)
    if exported:
        output_status = "Three separate Parquet tables saved."
    elif any(check.name == "Parquet export" for check in result.checks):
        output_status = "Export did not complete; output files may be from different runs."
    else:
        output_status = "Outputs were not refreshed; any existing output files belong to an earlier run."
    lines = [
        "# Data processing", "",
        f"Result: {failures} failed checks; {warnings} warnings. {output_status}",
        "", "Run from the project folder: `uv run src/run_pipeline.py`.", "",
        "## Validation", "", "| Check | Status | Finding |", "|---|---|---|",
    ]
    lines.extend(f"| {_cell(check.name)} | {check.status} | {_cell(check.detail)} |" for check in result.checks)
    lines += [
        "", "## Processing choices", "",
        f"Coverage dates and the expected Exposure formula follow the [CASdatasets documentation]({DOCUMENTATION_URL}). "
        "R dates are converted from days since 1970-01-01. BeginDate/EndDate use datetime64[ns] in all three tables.",
        "",
        "PolicyID/LicNb use strings. Year, VehiclNb, ClaimNb, CompRate, and SettlYear use nullable integers; "
        "Exposure/ClaimCharge use nullable floats. CompanyCreation maps No/Yes to False/True; "
        "DirectComp maps 0/1 to False/True (nullable booleans).",
        "",
        "Policy category labels use shared unordered categories, including VehiclPower (P1-P11 labels), "
        "VehiclAge, Deduc, and SumInsured. Unknown is retained as a supplied category, not treated as a null. "
        "Category levels are aligned using observed labels from both policy files; no numeric ranks are assigned.",
        "",
        f"ExposureMismatch flags abs(Exposure - (EndDate - BeginDate) / 365) > {tolerance:g} "
        "with no relative tolerance. The 0.005 comparison is a separate rounding benchmark, not an exemption. "
        "ExposureFromDates stores the date calculation; ExposureDifference stores supplied minus calculated exposure. "
        "An uncheckable row has a missing flag. All three diagnostic columns appear in both policy outputs.",
        "",
        "Uniqueness is checked on (PolicyID, LicNb, Year, BeginDate, EndDate) in each policy table. "
        "Duplicate combinations are flagged and retained. "
        "No rows are concatenated, joined, dropped, or deduplicated; missing values and nonpositive claim charges are retained. "
        "FAIL prevents export; WARN preserves the data for review.",
    ]
    if exported:
        lines += ["", "## Outputs", "", "| File in data/processed | Rows | Columns |", "|---|---:|---:|"]
        for name, frame in result.tables.items():
            lines.append(f"| {name}.parquet | {len(frame):,} | {len(frame.columns)} |")
        examples = []
        for name in ("clean_train_policy", "clean_test_policy"):
            frame = result.tables[name]
            flagged = frame.loc[frame["ExposureMismatch"].fillna(False)]
            indices = flagged["ExposureDifference"].abs().nlargest(2).index
            for _, row in flagged.loc[indices].iterrows():
                examples.append(
                    f"| {name} | {row['PolicyID']} | {row['LicNb']} | "
                    f"{row['BeginDate']:%Y-%m-%d} | {row['EndDate']:%Y-%m-%d} | "
                    f"{row['Exposure']:.6f} | {row['ExposureFromDates']:.6f} | {row['ExposureDifference']:.6f} |"
                )
        if examples:
            lines += [
                "", "## Largest exposure discrepancies", "",
                "| Table | PolicyID | LicNb | BeginDate | EndDate | Supplied | From dates | Difference |",
                "|---|---|---|---|---|---:|---:|---:|", *examples,
            ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_data(
    project_root: Path | str | None = None,
    exposure_tolerance: float = DEFAULT_EXPOSURE_TOLERANCE,
) -> ProcessingResult:
    """Process raw files and return clean tables keyed by their output names.

    Write data/processed/clean_*.parquet and results/data_processing.md.
    Missing files, invalid schemas/types, or failed integrity checks produce a
    report and raise DataValidationError without refreshing processed outputs.
    """
    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[2]
    report_path = root / "results" / "data_processing.md"
    try:
        result = clean_data(load_raw_data(root / "data" / "raw"), exposure_tolerance)
    except (OSError, ValueError, TypeError) as exc:
        result = ProcessingResult({}, [Check("Input loading/validation", "FAIL", str(exc))])
        _write_report(report_path, result, exposure_tolerance, exported=False)
        raise DataValidationError(f"{exc} See {report_path}") from exc
    if result.has_errors:
        _write_report(report_path, result, exposure_tolerance, exported=False)
        raise DataValidationError(f"Data validation failed. See {report_path}")

    processed_dir = root / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    staged_paths = {}
    try:
        # Complete serialization before replacing outputs from a previous run.
        # Stage beside the outputs to inherit normal project permissions on Windows.
        for name, frame in result.tables.items():
            staged_paths[name] = processed_dir / f".{name}-{uuid4().hex}.parquet"
            frame.to_parquet(staged_paths[name], engine="pyarrow", index=False)
        for name, path in staged_paths.items():
            path.replace(processed_dir / f"{name}.parquet")
    except (OSError, ValueError, TypeError) as exc:
        result.checks.append(Check("Parquet export", "FAIL", str(exc)))
        _write_report(report_path, result, exposure_tolerance, exported=False)
        raise DataValidationError(f"Output writing failed. See {report_path}") from exc
    finally:
        for path in staged_paths.values():
            path.unlink(missing_ok=True)
    _write_report(report_path, result, exposure_tolerance, exported=True)
    return result
