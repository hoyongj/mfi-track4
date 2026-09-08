"""Load, type, validate, and save the three separate Pricing Game 2016 tables.

Schema reference: https://dutangc.github.io/CASdatasets/reference/pricingame.html
Claim input checks precede removal of rows with ClaimCharge <= 0. The tables
remain separate; no deduplication or imputation is performed. Structural/type
errors and broken claim checks prevent export.
ClaimNbClean counts positive retained claim rows per training coverage period.
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
POLICY_KEY_COLUMNS = ("PolicyID", "LicNb", "Year", "BeginDate", "EndDate")
CLAIM_KEY_COLUMNS = (
    "PolicyID", "LicNb", "Year", "BeginDate", "EndDate", "SettlYear", "ClaimCharge",
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
OUTPUT_NAMES = {
    "pg16trainpol": "clean_train_policy",
    "pg16trainclaim": "clean_train_claim",
    "pg16test": "clean_test_policy",
}
# Meanings summarize the official PG16 reference. Statistical classifications
# are processing interpretations, informed by the definitions and observed labels.
VARIABLE_DEFINITIONS = {
    "Year": ("Numerical (discrete year)", "Calendar year to which coverage applies."),
    "BeginDate": ("Temporal (date)", "Date when coverage starts."),
    "EndDate": ("Temporal (date)", "Date when coverage ends."),
    "Exposure": ("Numerical (continuous fraction)", "Covered fraction of a year: (EndDate - BeginDate) / 365."),
    "PolicyID": ("Identifier (nominal)", "Identifier assigned to a policy."),
    "LicNb": ("Identifier (nominal)", "Vehicle licence identifier."),
    "PolicyAgeCateg": ("Categorical (ordinal age bands)", "Age bracket of the policy."),
    "PolicyCateg": ("Categorical (nominal)", "Policy classification."),
    "CompanyCreation": ("Binary indicator (nominal)", "Indicator of company creation."),
    "FleetMgt": ("Categorical (nominal)", "Fleet-management grouping."),
    "FleetSizeCateg": ("Categorical (nominal codes)", "Grouping by fleet size."),
    "Area": ("Categorical (nominal)", "Geographic zone."),
    "PayFreq": ("Categorical (ordinal frequency labels)", "How frequently payments occur."),
    "VehiclAge": ("Categorical (ordinal age bands)", "Grouping by vehicle age."),
    "VehiclNb": ("Numerical (discrete count)", "Count of vehicles."),
    "VehiclCateg": ("Categorical (nominal)", "Vehicle classification."),
    "VehiclPower": ("Categorical (nominal codes)", "Vehicle power."),
    "Deduc": ("Categorical (ordinal amount bands)", "Grouping by deductible amount."),
    "SumInsured": ("Categorical (ordinal bands; Unknown unranked)", "Grouping by insured amount."),
    "BusinessType": ("Categorical (nominal)", "Business classification."),
    "ChannelDist": ("Categorical (nominal)", "Channel used for distribution."),
    "ClaimNb": ("Numerical (discrete count)", "Count of claims."),
    "ClaimCharge": ("Numerical (continuous monetary amount)", "Charge associated with a claim."),
    "DirectComp": (
        "Binary indicator (nominal)",
        "Under IDA, indicates direct reimbursement to the insured, with possible later recovery from the other insurer.",
    ),
    "CompRate": ("Numerical (percentage)", "Compensation expressed as a percentage."),
    "SettlYear": ("Numerical (discrete year)", "Year in which settlement occurs."),
}
DERIVED_VARIABLE_DEFINITIONS = {
    "ClaimNbClean": (
        "Numerical (discrete count)",
        "Number of positive-charge cleaned claim rows matching the training policy's "
        "(PolicyID, LicNb, Year, BeginDate, EndDate); zero when none match. "
        "Missing for an incomplete or nonunique policy key.",
    ),
    "ExposureFromDates": (
        "Numerical (continuous fraction)", "Coverage duration in days divided by 365, calculated by this pipeline.",
    ),
    "ExposureDifference": (
        "Numerical (continuous signed difference)", "Supplied Exposure minus ExposureFromDates.",
    ),
    "ExposureMismatch": (
        "Binary indicator (nominal)", "True when the absolute exposure difference exceeds the configured tolerance; missing if uncheckable.",
    ),
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

    @property
    def variable_inventory(self) -> pd.DataFrame:
        """Describe every column currently present in the returned tables."""
        return describe_variables(self.tables)


def _infer_variable_type(values: pd.Series) -> str:
    """Conservative fallback for columns absent from the documented schema."""
    dtype = values.dtype
    if values.isna().all():
        return "Undetermined (all missing)"
    if pd.api.types.is_bool_dtype(dtype):
        return "Binary (boolean storage)"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "Temporal (date/time)"
    if pd.api.types.is_timedelta64_dtype(dtype):
        return "Temporal (duration)"
    if isinstance(dtype, pd.CategoricalDtype):
        return "Categorical (ordered storage)" if dtype.ordered else "Categorical (nominal storage)"
    if pd.api.types.is_integer_dtype(dtype):
        return "Numerical (integer storage; role unverified)"
    if pd.api.types.is_numeric_dtype(dtype):
        return "Numerical (numeric storage; role unverified)"
    return "Text/object (role unverified)"


def describe_variables(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Detect every actual column, including unexpected and derived variables.

    Return one metadata row per table/column without modifying the data. Dtypes,
    distinct counts, and missing counts are measured; semantic classifications
    use the documented meanings and observed labels. Undocumented columns get a
    conservative storage-based classification with no invented official meaning.
    A two-valued count remains numerical; binary flags and categorical variables
    with two observed levels are identified separately.
    """
    rows = []
    for table_name, frame in tables.items():
        for name, values in frame.items():
            distinct = int(values.nunique(dropna=True))
            if name in VARIABLE_DEFINITIONS:
                kind, meaning = VARIABLE_DEFINITIONS[name]
                origin, basis = "Official", "Definition and observed labels"
            elif name in DERIVED_VARIABLE_DEFINITIONS:
                kind, meaning = DERIVED_VARIABLE_DEFINITIONS[name]
                origin, basis = "Derived", "Pipeline definition"
            else:
                kind = _infer_variable_type(values)
                meaning = "No definition in the cited PG16 reference; meaning requires review."
                origin, basis = "Undocumented", "Storage inference only"
            if kind.startswith("Categorical") and distinct == 2:
                kind += "; binary observed"
            rows.append({
                "table": table_name, "variable": name, "dtype": str(values.dtype),
                "statistical_type": kind, "distinct": distinct,
                "missing": int(values.isna().sum()), "origin": origin,
                "classification_basis": basis, "meaning": meaning,
            })
    return pd.DataFrame(rows, columns=[
        "table", "variable", "dtype", "statistical_type", "distinct", "missing",
        "origin", "classification_basis", "meaning",
    ])


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
    incomplete = frame[list(POLICY_KEY_COLUMNS)].isna().any(axis=1)
    duplicates = int(frame.loc[~incomplete].duplicated(list(POLICY_KEY_COLUMNS)).sum())
    checks.append(Check(
        f"{name}: policy combination uniqueness", "WARN" if duplicates or incomplete.any() else "PASS",
        f"{duplicates:,} duplicate occurrences beyond the first for ({', '.join(POLICY_KEY_COLUMNS)}); "
        f"{int(incomplete.sum()):,} incomplete keys not checkable.",
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


def _process_claims(claims: pd.DataFrame, checks: list[Check]) -> pd.DataFrame:
    """Check the claim key before/after removing nonpositive charges.

    Missing charges are retained, and incomplete keys are reported as
    uncheckable. Duplicate keys do not themselves cause row removal.
    """
    nonpositive = claims["ClaimCharge"].le(0).fillna(False)
    filtered = claims.loc[~nonpositive].copy().reset_index(drop=True)
    details = []
    has_warning = False
    for stage, frame in (("Before filtering", claims), ("After filtering", filtered)):
        incomplete = frame[list(CLAIM_KEY_COLUMNS)].isna().any(axis=1)
        duplicates = int(frame.loc[~incomplete].duplicated(list(CLAIM_KEY_COLUMNS)).sum())
        has_warning = has_warning or bool(duplicates or incomplete.any())
        details.append(
            f"{stage}: {duplicates:,} duplicate occurrences beyond the first among {len(frame):,} rows; "
            f"{int(incomplete.sum()):,} incomplete keys not checkable"
        )
    checks.append(Check(
        "Claim combination uniqueness", "WARN" if has_warning else "PASS", "; ".join(details) + ".",
    ))
    negative = int(claims["ClaimCharge"].lt(0).sum())
    zero = int(claims["ClaimCharge"].eq(0).sum())
    checks.append(Check(
        "ClaimCharge filter", "PASS",
        f"{len(claims):,} input rows; removed {negative:,} negative and {zero:,} zero charges "
        f"({negative + zero:,} rows); retained {len(filtered):,} rows, including "
        f"{int(filtered['ClaimCharge'].isna().sum()):,} missing charges. Supplied policy ClaimNb values are unchanged.",
    ))
    return filtered


def _add_clean_claim_counts(policy: pd.DataFrame, claims: pd.DataFrame, checks: list[Check]) -> None:
    """Map aggregated positive claim counts onto complete, unique policy keys.

    Preserve every policy row and the supplied ClaimNb. Uncheckable policy keys
    receive a missing count; positive claims without one unambiguous match fail
    validation. Retained duplicate claim rows each contribute to the count.
    """
    key = list(POLICY_KEY_COLUMNS)
    assignable = policy[key].notna().all(axis=1) & ~policy.duplicated(key, keep=False)
    policy_index = pd.MultiIndex.from_frame(policy.loc[assignable, key])
    positive = claims.loc[claims["ClaimCharge"].gt(0).fillna(False)]
    positive_index = pd.MultiIndex.from_frame(positive[key])
    unmatched = int((~positive_index.isin(policy_index)).sum())
    checks.append(Check(
        "ClaimNbClean: matching", "FAIL" if unmatched else "PASS",
        f"{len(positive) - unmatched:,}/{len(positive):,} positive-charge cleaned claim rows match "
        f"one complete, unique ({', '.join(key)}) policy key; {unmatched:,} unassignable rows.",
    ))

    # Aggregate first, then map counts without multiplying or removing policy rows.
    counts = positive.groupby(key, sort=False).size().reindex(policy_index, fill_value=0)
    policy["ClaimNbClean"] = pd.Series(pd.NA, index=policy.index, dtype="Int64")
    policy.loc[assignable, "ClaimNbClean"] = counts.astype("Int64").array
    clean_counts = policy["ClaimNbClean"]
    total, missing = int(clean_counts.sum()), int(clean_counts.isna().sum())
    changed = int(clean_counts.ne(policy["ClaimNb"]).sum())
    checks.append(Check(
        "ClaimNbClean: counts", "FAIL" if total != len(positive) else "WARN" if missing else "PASS",
        f"sum(ClaimNbClean) = {total:,}; positive-charge cleaned claim rows = {len(positive):,}; "
        f"{int(clean_counts.eq(0).sum()):,} policy rows have zero counts; {missing:,} uncheckable counts; "
        f"{changed:,} counts differ from supplied ClaimNb, which is unchanged. Storage: Int64.",
    ))


def clean_data(
    raw_tables: dict[str, pd.DataFrame],
    exposure_tolerance: float = DEFAULT_EXPOSURE_TOLERANCE,
) -> ProcessingResult:
    """Return separate typed tables and findings without modifying inputs.

    WARN findings are retained for review. A result containing FAIL findings
    must not be used as clean output; process_data enforces that rule on export.
    ExposureMismatch is nullable when dates or exposure are missing.
    Claim relationships/counts are checked before removing nonpositive charges;
    supplied policy ClaimNb values and missing claim charges are preserved.
    ClaimNbClean is then derived for training policies from positive cleaned claims.
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
        reserved = sorted(set(DERIVED_VARIABLE_DEFINITIONS) & set(frame.columns))
        schema_error = bool(missing or duplicate_columns or reserved or frame.empty)
        checks.append(Check(
            f"{name}: columns", "FAIL" if schema_error else "WARN" if extra else "PASS",
            f"{len(frame):,} rows, {len(frame.columns)} columns; "
            f"missing: {missing or 'none'}; unexpected: {extra or 'none'}"
            + (f"; duplicate columns: {duplicate_columns}" if duplicate_columns else "")
            + (f"; reserved derived columns already present: {reserved}" if reserved else "")
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
        f"test-only: {sorted(set(test.columns) - set(train_features)) or 'none'}. "
        "ClaimNb and derived ClaimNbClean are training outcomes.",
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
        "Claim-to-policy relationship (before filtering)", "FAIL" if orphan.any() else "PASS",
        f"{int(orphan.sum()):,} claim rows have a missing or unknown PolicyID.",
    ))
    counts_valid = train["ClaimNb"].notna().all() and (train["ClaimNb"] >= 0).all()
    total = int(train["ClaimNb"].sum()) if counts_valid else None
    checks.append(Check(
        "Claim count reconciliation (before filtering)", "PASS" if total == len(claims) else "FAIL",
        f"Claim rows = {len(claims):,}; sum(training ClaimNb) = {total:,}." if total is not None
        else "ClaimNb contains missing or negative values; a complete total cannot be reconciled.",
    ))
    if counts_valid and not orphan.any():
        expected_counts = train.groupby("PolicyID")["ClaimNb"].sum()
        observed_counts = claims.groupby("PolicyID").size().reindex(expected_counts.index, fill_value=0)
        differences = int((expected_counts != observed_counts).sum())
        checks.append(Check(
            "Claim counts per PolicyID (before filtering)", "FAIL" if differences else "PASS",
            f"{differences:,} IDs disagree after aggregating counts for validation only.",
        ))
    tables["pg16trainclaim"] = _process_claims(claims, checks)
    _add_clean_claim_counts(train, tables["pg16trainclaim"], checks)

    # Align feature order while retaining any unexpected columns for review.
    for name, frame in tables.items():
        original_columns = list(REQUIRED_COLUMNS[name])
        if name == "pg16trainpol":
            original_columns.append("ClaimNbClean")
        remainder = [column for column in frame if column not in original_columns]
        outputs[OUTPUT_NAMES[name]] = frame.loc[:, original_columns + remainder]
    return result


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _variable_inventory_lines(inventory: pd.DataFrame) -> list[str]:
    """Summarize repeated field definitions once, retaining per-table counts."""
    table_order = inventory["table"].drop_duplicates().tolist()
    table_labels = {
        "clean_train_policy": "Train", "clean_train_claim": "Claims", "clean_test_policy": "Test",
    }
    count_heading = "/".join(table_labels.get(name, name) for name in table_order)
    original_count = inventory.loc[inventory["origin"] == "Official", "variable"].nunique()
    derived_count = inventory.loc[inventory["origin"] == "Derived", "variable"].nunique()
    lines = [
        "", "## Variable inventory", "",
        f"Detected {inventory['variable'].nunique()} distinct variable names across "
        f"{len(inventory):,} table columns: {original_count} documented source variables and "
        f"{derived_count} derived variables. Any undocumented variables are listed separately.",
        "",
        "Train = clean_train_policy; Claims = clean_train_claim; Test = clean_test_policy. "
        "Distinct counts exclude missing values; '-' means the variable is absent. "
        "Storage dtypes are detected after processing.",
        "",
        f"Official meanings below summarize the [PG16 reference]({DOCUMENTATION_URL}#format). "
        "Statistical classifications are processing interpretations of definitions and observed labels. "
        "Ordinal describes interpretable bands or frequencies; categorical storage remains unordered. "
        "'Binary observed' means two observed category levels, without asserting only two are possible. "
        "Counts retain their numerical role even when only two values occur.",
    ]
    for origin, title, meaning_heading in (
        ("Official", "Source variables", "Official meaning (summary)"),
        ("Undocumented", "Undocumented variables", "Meaning"),
        ("Derived", "Derived variables", "Pipeline meaning"),
    ):
        subset = inventory.loc[inventory["origin"] == origin]
        if subset.empty:
            continue
        lines += [
            "", f"### {title}", "",
            f"| Variable | Statistical classification | Storage dtype | Distinct {count_heading} | {meaning_heading} |",
            "|---|---|---|---|---|",
        ]
        for name, group in subset.groupby("variable", sort=False):
            def summary(column: str) -> str:
                distinct = group[column].drop_duplicates().tolist()
                if len(distinct) == 1:
                    return str(distinct[0])
                return "; ".join(
                    f"{table_labels.get(row['table'], row['table'])}: {row[column]}"
                    for _, row in group.iterrows()
                )

            counts = dict(zip(group["table"], group["distinct"]))
            count_text = " / ".join(f"{counts[table]:,}" if table in counts else "-" for table in table_order)
            cells = [name, summary("statistical_type"), summary("dtype"), count_text, summary("meaning")]
            lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    lines += [
        "",
        "Interpretation notes: PolicyAgeCateg, VehiclAge, and Deduc contain interpretable age/amount bands; "
        "PayFreq contains year/semester/quarter labels. SumInsured's monetary bands have a natural order, "
        "while its Unknown category has no rank. FleetSizeCateg (S1/S2) and VehiclPower (P1-P11) "
        "retain nominal treatment because their code meanings/order are undisclosed. "
        "VehiclNb values 1/2 remain vehicle counts. CompRate's observed 0/50/100 values remain percentages. "
        "SettlYear includes 0, whose meaning the reference does not explain. "
        "Derived variables have pipeline definitions, not official dataset definitions.",
    ]
    return lines


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
        "", "Run from the project folder: `uv run src/run_pipeline.py --stage processing`.", "",
        "This generated report records input validation, cleaning rules, variable meanings, and output checks. "
        "Further analysis of coverage-period linkage and claim counts after filtering is in "
        "[claim_table_checks.md](claim_table_checks.md).",
        "",
        "## Validation", "", "| Check | Status | Finding |", "|---|---|---|",
    ]
    lines.extend(f"| {_cell(check.name)} | {check.status} | {_cell(check.detail)} |" for check in result.checks)
    lines += [
        "", "## Processing choices", "",
        f"Coverage dates and the expected Exposure formula follow the [CASdatasets documentation]({DOCUMENTATION_URL}). "
        "R dates are converted from days since 1970-01-01. BeginDate/EndDate use datetime64[ns] in all three tables.",
        "",
        "PolicyID/LicNb use strings. Year, VehiclNb, ClaimNb, ClaimNbClean, CompRate, and SettlYear use nullable integers; "
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
        f"Policy uniqueness is checked on ({', '.join(POLICY_KEY_COLUMNS)}) separately in the training and test policy tables. "
        f"Claim uniqueness is checked on ({', '.join(CLAIM_KEY_COLUMNS)}) before and after filtering. "
        "Incomplete keys are reported as uncheckable. Duplicate combinations are flagged without deduplication.",
        "",
        "The duplicate claim combinations table and its interpretation are in "
        "[Question 2 of claim_table_checks.md](claim_table_checks.md#duplicate-claim-combinations).",
        "",
        "Project interpretation: negative or zero claim charges reflect claims where the insured driver is not liable "
        "and legal recourse applies.",
        "",
        "Claim rows with ClaimCharge <= 0 are removed from clean_train_claim. "
        "Claim relationship and count reconciliation checks use the input claims before removal. "
        "Policy ClaimNb retains its supplied counts, so the filtered claim row count is lower by the number removed. "
        "Missing charges are retained and reported. Policy rows and raw files are preserved; "
        "tables are not joined or concatenated, and missing values are not imputed. "
        "FAIL prevents export; WARN records findings for review.",
        "",
        "ClaimNbClean counts rows with ClaimCharge > 0 in clean_train_claim for each "
        f"({', '.join(POLICY_KEY_COLUMNS)}) training policy key. Counts are aggregated before mapping to policies. "
        "Complete, unique policy keys with no positive claims receive zero; retained duplicate claim rows each count, "
        "and missing charges do not count. Incomplete or nonunique policy keys receive missing counts; "
        "positive claims without an unambiguous policy match prevent export. "
        "This derived training outcome is absent from test data because no test claim outcomes are supplied. "
        "Supplied ClaimNb and every policy row are preserved.",
    ]
    if exported:
        lines.extend(_variable_inventory_lines(result.variable_inventory))
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
