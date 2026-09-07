# Claim table checks

Read-only analysis of `data/processed/clean_train_claim.parquet` (3,969 rows,
9 columns) against `data/processed/clean_train_policy.parquet` (87,228 rows,
25 columns). All saved claim charges are positive; none are missing.

[data_processing.md](data_processing.md) records the cleaning rules, removal totals
and liability/recourse explanation, official variable meanings, types, and pipeline
validation. This report preserves the three original questions below, with answers
updated for the cleaned claims. Re-run the code below when processed outputs change.

## Question 1: What do BeginDate/EndDate mean in the claim table?

They are the start and end dates of the associated policy coverage period. Use
`(PolicyID, LicNb, Year, BeginDate, EndDate)` to identify that period. Counting
candidate policy rows for each retained claim gives:

| Fields used for matching | Exactly one candidate | Multiple candidates | No candidate |
|---|---:|---:|---:|
| PolicyID, LicNb, Year | 3,580 | 389 | 0 |
| PolicyID, LicNb, Year, BeginDate, EndDate | 3,969 | 0 | 0 |

Both dates match exactly for every retained claim. This supports the coverage-date
interpretation in the [variable inventory](data_processing.md#variable-inventory).
Several policy periods can share the same policy, vehicle, and year; including the
dates resolves the ambiguity without choosing an arbitrary policy row.

## Question 2: One row per accident, or one row per payment?

The available fields do not resolve this distinction. These nine columns contain
no claim/accident identifier or payment-sequence field. Multiple claims can share
a coverage period, so repeated coverage keys and differences in SettlYear or
ClaimCharge do not establish whether rows represent separate accidents or
separate payments.

The retained claims cover 3,727 distinct five-field policy periods. Of these,
217 periods contain multiple claim rows (459 rows in total).

| Claim rows in a period | Number of periods |
|---:|---:|
| 1 | 3,510 |
| 2 | 197 |
| 3 | 18 |
| 4 | 1 |
| 7 | 1 |

The required seven-field claim uniqueness check and its examples belong in the
[processing report](data_processing.md#duplicate-claim-combinations).

`SettlYear=0` occurs in 39 retained rows. Its meaning remains unresolved in the
[variable inventory](data_processing.md#variable-inventory); it is not used here
to classify claims as settled or unsettled.

## Question 3: Does ClaimNb reconcile at (PolicyID, LicNb, Year)?

Yes, before filtering, when ClaimNb is summed across all coverage periods for
each key. After cleaning, retained claim counts are lower because negative and
zero charges were excluded. The corresponding policy rows and supplied ClaimNb
values remain unchanged.

The [processing choices](data_processing.md#processing-choices) record the project
explanation: these nonpositive charges concern claims where the insured driver is
not liable and legal recourse applies.

A read-only check also confirms exact reconciliation before filtering at the
full five-field coverage key. At that finer level, compare retained claim counts
with ClaimNb across **all** training policy rows, assigning a count of zero to
periods with no retained claims:

| Supplied ClaimNb minus retained claim rows | Number of policy rows |
|---:|---:|
| 0 | 86,640 |
| 1 | 577 |
| 2 | 11 |

The total difference is 599 rows. For every coverage period, the difference
equals exactly the number of claims removed under the
[documented filter](data_processing.md#processing-choices). All 588 affected
policy rows remain: 523 now have no retained claims, and 65 still have at least one.
Supplied ClaimNb remains unchanged.

For a comparison at `(PolicyID, LicNb, Year)`, **sum ClaimNb across all matching
coverage periods**. Using the first or maximum value loses count information.
With this aggregation, 588 of 79,837 policy groups have a lower retained claim
count; the total difference is again 599. These differences are fully accounted
for by filtering.

## Reproduce these checks

Run this PowerShell block from the project folder. It reuses the pipeline's type
conversion for the raw input comparison and writes no data or reports.

```powershell
@'
from pathlib import Path
import sys
import pandas as pd

sys.path.insert(0, str(Path('src').resolve()))
from track_4.data_processing import (
    POLICY_KEY_COLUMNS, load_raw_data, _convert_table,
)

policy = pd.read_parquet('data/processed/clean_train_policy.parquet')
claim = pd.read_parquet('data/processed/clean_train_claim.parquet')
key = list(POLICY_KEY_COLUMNS)
short_key = ['PolicyID', 'LicNb', 'Year']

raw = load_raw_data(Path('data/raw'))
input_claim = raw['pg16trainclaim'].copy()
input_policy = raw['pg16trainpol'].copy()
checks = []
_convert_table('pg16trainclaim', input_claim, checks)
_convert_table('pg16trainpol', input_policy, checks)
assert all(check.status == 'PASS' for check in checks)
for frame in (policy, claim, input_claim):
    assert frame[key].notna().all().all()

# Verify that every policy row and its supplied count survives processing.
pd.testing.assert_frame_equal(
    policy[key + ['ClaimNb']], input_policy[key + ['ClaimNb']]
)
nonpositive = input_claim['ClaimCharge'].le(0).fillna(False)
removed = input_claim.loc[nonpositive]
pd.testing.assert_frame_equal(
    claim, input_claim.loc[~nonpositive].reset_index(drop=True)
)
assert claim['ClaimCharge'].notna().all() and claim['ClaimCharge'].gt(0).all()
print('Saved table shapes:', policy.shape, claim.shape)

# Index lookups count candidates without joining individual rows into policy.
policy_index = pd.MultiIndex.from_frame(policy[key])
assert policy_index.is_unique
for label, frame in [('input', input_claim), ('retained', claim)]:
    matched = pd.MultiIndex.from_frame(frame[key]).isin(policy_index)
    assert matched.all()
    print(label, 'exact coverage matches:', int(matched.sum()))
short_sizes = policy.groupby(short_key).size()
candidates = short_sizes.reindex(
    pd.MultiIndex.from_frame(claim[short_key]), fill_value=0
)
print('Short-key candidate counts:', candidates.value_counts().sort_index().to_dict())

# Multiplicity within a coverage period is separate from claim-key uniqueness.
sizes = claim.groupby(key).size()
repeated = sizes[sizes.gt(1)]
print('Periods / repeated periods / rows in repeats:',
      len(sizes), len(repeated), int(repeated.sum()))
print('Rows per period:', sizes.value_counts().sort_index().to_dict())
print('Retained SettlYear=0 rows:', int(claim['SettlYear'].eq(0).sum()))

# Include every policy period, including those with zero retained claims.
expected = policy.set_index(key)['ClaimNb']
observed = sizes.reindex(expected.index, fill_value=0)
before = input_claim.groupby(key).size().reindex(expected.index, fill_value=0)
dropped = removed.groupby(key).size().reindex(expected.index, fill_value=0)
gap = expected - observed
pd.testing.assert_series_equal(expected, before, check_names=False, check_dtype=False)
pd.testing.assert_series_equal(gap, dropped, check_names=False, check_dtype=False)
print('Count gaps by period:', gap.value_counts().sort_index().to_dict())
print('Total gap / affected periods:', int(gap.sum()), int(gap.gt(0).sum()))
print('Affected periods with zero / some retained claims:',
      int((gap.gt(0) & observed.eq(0)).sum()),
      int((gap.gt(0) & observed.gt(0)).sum()))

short_expected = policy.groupby(short_key)['ClaimNb'].sum()
short_observed = claim.groupby(short_key).size().reindex(short_expected.index, fill_value=0)
short_gap = short_expected - short_observed
print('Short-key groups / count differences / total gap:',
      len(short_expected), int(short_gap.ne(0).sum()), int(short_gap.sum()))
'@ | uv run --with 'pandas==3.0.5' --with 'rdata==1.1.0' --with 'pyarrow==25.0.1' python -B -
```
