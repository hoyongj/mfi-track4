# Data processing

Result: 0 failed checks; 4 warnings. Three separate Parquet tables saved.

Run from the project folder: `uv run src/run_pipeline.py`.

## Validation

| Check | Status | Finding |
|---|---|---|
| pg16trainpol: columns | PASS | 87,228 rows, 22 columns; missing: none; unexpected: none. |
| pg16trainclaim: columns | PASS | 4,568 rows, 9 columns; missing: none; unexpected: none. |
| pg16test: columns | PASS | 32,772 rows, 21 columns; missing: none; unexpected: none. |
| pg16trainpol: types | PASS | Identifiers, dates, numeric fields, and binary fields converted without new missing values. |
| pg16trainpol: missing values | PASS | 0 missing cells across all columns. |
| pg16trainclaim: types | PASS | Identifiers, dates, numeric fields, and binary fields converted without new missing values. |
| pg16trainclaim: missing values | PASS | 0 missing cells across all columns. |
| pg16test: types | PASS | Identifiers, dates, numeric fields, and binary fields converted without new missing values. |
| pg16test: missing values | PASS | 0 missing cells across all columns. |
| Train/test feature columns | PASS | Train-only: none; test-only: none. ClaimNb is a training outcome. |
| Categorical levels | WARN | SumInsured: train-only ['<=7.6 Keur'], test-only none; PolicyCateg: train-only ['C2'], test-only none |
| Train/test feature types | PASS | Mismatched types: none; shared unordered category definitions. |
| pg16trainpol: PolicyID present | PASS | 0 missing or blank identifiers. |
| pg16trainpol: policy combination uniqueness | PASS | 0 duplicate occurrences of (PolicyID, LicNb, Year, BeginDate, EndDate). |
| pg16trainpol: dates and ranges | PASS | 0 reversed periods; 0 rows with negative exposure or vehicle count below one. |
| pg16trainpol: Exposure | WARN | 82,435/87,228 comparable rows differ; 0 not checkable; 18,052 exceed 0.005 + tolerance; max absolute difference 0.165616438. Supplied Exposure retained. |
| pg16test: PolicyID present | PASS | 0 missing or blank identifiers. |
| pg16test: policy combination uniqueness | PASS | 0 duplicate occurrences of (PolicyID, LicNb, Year, BeginDate, EndDate). |
| pg16test: dates and ranges | PASS | 0 reversed periods; 0 rows with negative exposure or vehicle count below one. |
| pg16test: Exposure | WARN | 32,572/32,772 comparable rows differ; 0 not checkable; 7,918 exceed 0.005 + tolerance; max absolute difference 0.754794521. Supplied Exposure retained. |
| Claim-to-policy relationship | PASS | 0 claim rows have a missing or unknown PolicyID. |
| Claim count reconciliation | PASS | Claim rows = 4,568; sum(training ClaimNb) = 4,568. |
| Claim counts per PolicyID | PASS | 0 IDs disagree after aggregating counts for validation only. |
| ClaimCharge values | WARN | 597 negative and 2 zero charges; retained without adjustment. |

## Processing choices

Coverage dates and the expected Exposure formula follow the [CASdatasets documentation](https://dutangc.github.io/CASdatasets/reference/pricingame.html). R dates are converted from days since 1970-01-01. BeginDate/EndDate use datetime64[ns] in all three tables.

PolicyID/LicNb use strings. Year, VehiclNb, ClaimNb, CompRate, and SettlYear use nullable integers; Exposure/ClaimCharge use nullable floats. CompanyCreation maps No/Yes to False/True; DirectComp maps 0/1 to False/True (nullable booleans).

Policy category labels use shared unordered categories, including VehiclPower (P1-P11 labels), VehiclAge, Deduc, and SumInsured. Unknown is retained as a supplied category, not treated as a null. Category levels are aligned using observed labels from both policy files; no numeric ranks are assigned.

ExposureMismatch flags abs(Exposure - (EndDate - BeginDate) / 365) > 1e-08 with no relative tolerance. The 0.005 comparison is a separate rounding benchmark, not an exemption. ExposureFromDates stores the date calculation; ExposureDifference stores supplied minus calculated exposure. An uncheckable row has a missing flag. All three diagnostic columns appear in both policy outputs.

Uniqueness is checked on (PolicyID, LicNb, Year, BeginDate, EndDate) in each policy table. Duplicate combinations are flagged and retained. No rows are concatenated, joined, dropped, or deduplicated; missing values and nonpositive claim charges are retained. FAIL prevents export; WARN preserves the data for review.

## Outputs

| File in data/processed | Rows | Columns |
|---|---:|---:|
| clean_train_policy.parquet | 87,228 | 25 |
| clean_train_claim.parquet | 4,568 | 9 |
| clean_test_policy.parquet | 32,772 | 24 |

## Largest exposure discrepancies

| Table | PolicyID | LicNb | BeginDate | EndDate | Supplied | From dates | Difference |
|---|---|---|---|---|---:|---:|---:|
| clean_train_policy | 2954 | 16114 | 2012-02-03 | 2012-04-03 | 0.330000 | 0.164384 | 0.165616 |
| clean_train_policy | 5515 | 29092 | 2012-05-16 | 2012-06-17 | 0.180000 | 0.087671 | 0.092329 |
| clean_test_policy | 104 | 536 | 2014-01-01 | 2014-09-30 | 1.500000 | 0.745205 | 0.754795 |
| clean_test_policy | 17983 | 29 | 2014-01-01 | 2014-11-30 | 0.920000 | 0.912329 | 0.007671 |
