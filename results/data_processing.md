# Data processing

Result: 0 failed checks; 4 warnings. Three separate Parquet tables saved.

Run from the project folder: `uv run src/run_pipeline.py`.

This generated report records input validation, cleaning rules, variable meanings, and output checks. Further analysis of coverage-period linkage and claim counts after filtering is in [claim_table_checks.md](claim_table_checks.md).

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
| pg16trainpol: policy combination uniqueness | PASS | 0 duplicate occurrences beyond the first for (PolicyID, LicNb, Year, BeginDate, EndDate); 0 incomplete keys not checkable. |
| pg16trainpol: dates and ranges | PASS | 0 reversed periods; 0 rows with negative exposure or vehicle count below one. |
| pg16trainpol: Exposure | WARN | 82,435/87,228 comparable rows differ; 0 not checkable; 18,052 exceed 0.005 + tolerance; max absolute difference 0.165616438. Supplied Exposure retained. |
| pg16test: PolicyID present | PASS | 0 missing or blank identifiers. |
| pg16test: policy combination uniqueness | PASS | 0 duplicate occurrences beyond the first for (PolicyID, LicNb, Year, BeginDate, EndDate); 0 incomplete keys not checkable. |
| pg16test: dates and ranges | PASS | 0 reversed periods; 0 rows with negative exposure or vehicle count below one. |
| pg16test: Exposure | WARN | 32,572/32,772 comparable rows differ; 0 not checkable; 7,918 exceed 0.005 + tolerance; max absolute difference 0.754794521. Supplied Exposure retained. |
| Claim-to-policy relationship (before filtering) | PASS | 0 claim rows have a missing or unknown PolicyID. |
| Claim count reconciliation (before filtering) | PASS | Claim rows = 4,568; sum(training ClaimNb) = 4,568. |
| Claim counts per PolicyID (before filtering) | PASS | 0 IDs disagree after aggregating counts for validation only. |
| Claim combination uniqueness | WARN | Before filtering: 3 duplicate occurrences beyond the first among 4,568 rows; 0 incomplete keys not checkable; After filtering: 3 duplicate occurrences beyond the first among 3,969 rows; 0 incomplete keys not checkable. |
| ClaimCharge filter | PASS | 4,568 input rows; removed 597 negative and 2 zero charges (599 rows); retained 3,969 rows, including 0 missing charges. Supplied policy ClaimNb values are unchanged. |

## Processing choices

Coverage dates and the expected Exposure formula follow the [CASdatasets documentation](https://dutangc.github.io/CASdatasets/reference/pricingame.html). R dates are converted from days since 1970-01-01. BeginDate/EndDate use datetime64[ns] in all three tables.

PolicyID/LicNb use strings. Year, VehiclNb, ClaimNb, CompRate, and SettlYear use nullable integers; Exposure/ClaimCharge use nullable floats. CompanyCreation maps No/Yes to False/True; DirectComp maps 0/1 to False/True (nullable booleans).

Policy category labels use shared unordered categories, including VehiclPower (P1-P11 labels), VehiclAge, Deduc, and SumInsured. Unknown is retained as a supplied category, not treated as a null. Category levels are aligned using observed labels from both policy files; no numeric ranks are assigned.

ExposureMismatch flags abs(Exposure - (EndDate - BeginDate) / 365) > 1e-08 with no relative tolerance. The 0.005 comparison is a separate rounding benchmark, not an exemption. ExposureFromDates stores the date calculation; ExposureDifference stores supplied minus calculated exposure. An uncheckable row has a missing flag. All three diagnostic columns appear in both policy outputs.

Policy uniqueness is checked on (PolicyID, LicNb, Year, BeginDate, EndDate) separately in the training and test policy tables. Claim uniqueness is checked on (PolicyID, LicNb, Year, BeginDate, EndDate, SettlYear, ClaimCharge) before and after filtering. Incomplete keys are reported as uncheckable. Duplicate combinations are flagged without deduplication.

Project interpretation: negative or zero claim charges reflect claims where the insured driver is not liable and legal recourse applies.

Claim rows with ClaimCharge <= 0 are removed from clean_train_claim. Claim relationship and count reconciliation checks use the input claims before removal. Policy ClaimNb retains its supplied counts, so the filtered claim row count is lower by the number removed. Missing charges are retained and reported. Policy rows and raw files are preserved; tables are not joined or concatenated, and missing values are not imputed. FAIL prevents export; WARN records findings for review.

## Variable inventory

Detected 29 distinct variable names across 58 table columns: 26 documented source variables and 3 pipeline diagnostics. Any undocumented variables are listed separately.

Train = clean_train_policy; Claims = clean_train_claim; Test = clean_test_policy. Distinct counts exclude missing values; '-' means the variable is absent. Storage dtypes are detected after processing.

Official meanings below summarize the [PG16 reference](https://dutangc.github.io/CASdatasets/reference/pricingame.html#format). Statistical classifications are processing interpretations of definitions and observed labels. Ordinal describes interpretable bands or frequencies; categorical storage remains unordered. 'Binary observed' means two observed category levels, without asserting only two are possible. Counts retain their numerical role even when only two values occur.

### Source variables

| Variable | Statistical classification | Storage dtype | Distinct Train/Claims/Test | Official meaning (summary) |
|---|---|---|---|---|
| Year | Numerical (discrete year) | Int64 | 3 / 3 / 1 | Calendar year to which coverage applies. |
| BeginDate | Temporal (date) | datetime64[ns] | 1,003 / 660 / 334 | Date when coverage starts. |
| EndDate | Temporal (date) | datetime64[ns] | 1,004 / 666 / 336 | Date when coverage ends. |
| PolicyAgeCateg | Categorical (ordinal age bands) | category | 6 / - / 6 | Age bracket of the policy. |
| CompanyCreation | Binary indicator (nominal) | boolean | 2 / - / 2 | Indicator of company creation. |
| FleetMgt | Categorical (nominal); binary observed | category | 2 / - / 2 | Fleet-management grouping. |
| Area | Categorical (nominal) | category | 6 / - / 6 | Geographic zone. |
| FleetSizeCateg | Categorical (nominal codes); binary observed | category | 2 / - / 2 | Grouping by fleet size. |
| PayFreq | Categorical (ordinal frequency labels) | category | 3 / - / 3 | How frequently payments occur. |
| Exposure | Numerical (continuous fraction) | Float64 | 93 / - / 94 | Covered fraction of a year: (EndDate - BeginDate) / 365. |
| VehiclAge | Categorical (ordinal age bands); binary observed | category | 2 / - / 2 | Grouping by vehicle age. |
| Deduc | Categorical (ordinal amount bands) | category | 6 / - / 6 | Grouping by deductible amount. |
| VehiclNb | Numerical (discrete count) | Int64 | 2 / - / 2 | Count of vehicles. |
| SumInsured | Categorical (ordinal bands; Unknown unranked) | category | 6 / - / 5 | Grouping by insured amount. |
| PolicyCateg | Categorical (nominal) | category | 4 / - / 3 | Policy classification. |
| VehiclCateg | Categorical (nominal) | category | 1 / - / 1 | Vehicle classification. |
| PolicyID | Identifier (nominal) | string | 18,037 / 3,062 / 12,521 | Identifier assigned to a policy. |
| BusinessType | Categorical (nominal) | category | 8 / - / 8 | Business classification. |
| ChannelDist | Categorical (nominal) | category | 3 / - / 3 | Channel used for distribution. |
| VehiclPower | Categorical (nominal codes) | category | 11 / - / 11 | Vehicle power. |
| LicNb | Identifier (nominal) | string | 68,485 / 3,691 / 29,958 | Vehicle licence identifier. |
| ClaimNb | Numerical (discrete count) | Int64 | 6 / - / - | Count of claims. |
| DirectComp | Binary indicator (nominal) | boolean | - / 2 / - | Under IDA, indicates direct reimbursement to the insured, with possible later recovery from the other insurer. |
| CompRate | Numerical (percentage) | Int64 | - / 3 / - | Compensation expressed as a percentage. |
| SettlYear | Numerical (discrete year) | Int64 | - / 5 / - | Year in which settlement occurs. |
| ClaimCharge | Numerical (continuous monetary amount) | Float64 | - / 3,806 / - | Charge associated with a claim. |

### Derived diagnostics

| Variable | Statistical classification | Storage dtype | Distinct Train/Claims/Test | Pipeline meaning |
|---|---|---|---|---|
| ExposureFromDates | Numerical (continuous fraction) | Float64 | 337 / - / 335 | Coverage duration in days divided by 365, calculated by this pipeline. |
| ExposureDifference | Numerical (continuous signed difference) | Float64 | 224 / - / 192 | Supplied Exposure minus ExposureFromDates. |
| ExposureMismatch | Binary indicator (nominal) | boolean | 2 / - / 2 | True when the absolute exposure difference exceeds the configured tolerance; missing if uncheckable. |

Interpretation notes: PolicyAgeCateg, VehiclAge, and Deduc contain interpretable age/amount bands; PayFreq contains year/semester/quarter labels. SumInsured's monetary bands have a natural order, while its Unknown category has no rank. FleetSizeCateg (S1/S2) and VehiclPower (P1-P11) retain nominal treatment because their code meanings/order are undisclosed. VehiclNb values 1/2 remain vehicle counts. CompRate's observed 0/50/100 values remain percentages. SettlYear includes 0, whose meaning the reference does not explain. The three derived diagnostics have pipeline definitions, not official dataset definitions.

## Outputs

| File in data/processed | Rows | Columns |
|---|---:|---:|
| clean_train_policy.parquet | 87,228 | 25 |
| clean_train_claim.parquet | 3,969 | 9 |
| clean_test_policy.parquet | 32,772 | 24 |

## Duplicate claim combinations

Up to five repeated combinations in the filtered claim output; Occurrences includes the first row.

| PolicyID | LicNb | Year | BeginDate | EndDate | SettlYear | ClaimCharge | Occurrences |
|---|---|---|---|---|---|---|---|
| 135 | 699 | 2012 | 2012-03-12 | 2012-06-30 | 2012 | 3639.7934673922023 | 2 |
| 5336 | 28265 | 2013 | 2013-01-02 | 2013-12-31 | 2014 | 1863.3606703405062 | 2 |
| 14499 | 69326 | 2013 | 2013-04-04 | 2013-08-31 | 2013 | 6996.211768431509 | 2 |

## Largest exposure discrepancies

| Table | PolicyID | LicNb | BeginDate | EndDate | Supplied | From dates | Difference |
|---|---|---|---|---|---:|---:|---:|
| clean_train_policy | 2954 | 16114 | 2012-02-03 | 2012-04-03 | 0.330000 | 0.164384 | 0.165616 |
| clean_train_policy | 5515 | 29092 | 2012-05-16 | 2012-06-17 | 0.180000 | 0.087671 | 0.092329 |
| clean_test_policy | 104 | 536 | 2014-01-01 | 2014-09-30 | 1.500000 | 0.745205 | 0.754795 |
| clean_test_policy | 17983 | 29 | 2014-01-01 | 2014-11-30 | 0.920000 | 0.912329 | 0.007671 |
