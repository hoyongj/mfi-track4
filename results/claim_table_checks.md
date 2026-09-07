# Claim table checks

Read-only investigation of `data/processed/clean_train_claim.parquet` (4,568 rows, 9
columns) against `data/processed/clean_train_policy.parquet` (87,228 rows, 25 columns).
No data, pipeline, or `data/` files were modified. Run with:

```
python -m venv .v && .v/bin/pip install pandas pyarrow
.v/bin/python results/claim_table_checks.py   # code reproduced in full below
```

## Preliminary: the join key is not unique on the policy side

Before answering the three questions, one fact affects all of them: **(PolicyID, LicNb,
Year) does not uniquely identify a policy row.** Of 87,228 policy rows, 14,270 sit in a
group that shares a (PolicyID, LicNb, Year) key with at least one other row (79,837
distinct key groups total). This is mid-year policy amendment: the same policy/year
splits into two or more sub-period rows, each with its own (BeginDate, EndDate).

Consequence: "the policy row a claim joins to" on (PolicyID, LicNb, Year) alone is
**ambiguous** — a claim can match 1 or more candidate policy rows on that key. Both
questions below report results accounting for this rather than silently picking one
candidate.

## Question 1 — What do BeginDate/EndDate mean in the claim table?

Joined each claim row to all policy rows sharing (PolicyID, LicNb, Year), then checked
whether the claim's (BeginDate, EndDate) matches the policy row's (BeginDate, EndDate).

- Every claim row (4,568 / 4,568) found at least one matching (PolicyID, LicNb, Year)
  key on the policy side — no orphan claims on this key.
- 4,117 claim rows matched to exactly **one** candidate policy row (unambiguous join);
  451 matched to more than one candidate (ambiguous join, because the policy side has
  multiple sub-period rows for that key).
- **Match rate: 4,568 / 4,568 = 100.00%** of claim rows have (BeginDate, EndDate) equal
  to (BeginDate, EndDate) of at least one candidate policy row.
- Restricting to only the 4,117 unambiguous single-candidate claims, the match rate is
  also 4,117 / 4,117 = **100.00%**.

**Verdict: BeginDate/EndDate in the claim table are policy coverage-period columns,
copied from the policy row, not accident dates.** The match is exact and total, not a
majority — there is no accident date in this data under any of the columns present.

## Question 2 — One row per accident, or one row per payment?

Checked uniqueness of (PolicyID, LicNb, Year, BeginDate, EndDate) within the claim
table (4,568 rows).

- Distinct combinations: 4,250. Rows sharing a duplicated combination: **604**, forming
  **286** duplicate-key groups.
- Group-size distribution among duplicate groups: 261 groups of size 2, 21 of size 3, 3
  of size 4, 1 of size 7.
- Three example duplicate groups (all columns) — same (PolicyID, LicNb, Year,
  BeginDate, EndDate), different SettlYear/ClaimCharge:

```
-- Group ('10081', '48630', 2012, 2012-01-01, 2012-12-31) --
      BeginDate  Year    EndDate  DirectComp  CompRate  SettlYear  ClaimCharge PolicyID  LicNb
2771 2012-01-01  2012 2012-12-31       False         0       2013  3155.185394    10081  48630
2772 2012-01-01  2012 2012-12-31       False         0       2012  1711.773118    10081  48630

-- Group ('10090', '48678', 2011, 2011-01-01, 2011-06-19) --
      BeginDate  Year    EndDate  DirectComp  CompRate  SettlYear  ClaimCharge PolicyID  LicNb
2774 2011-01-01  2011 2011-06-19       False         0       2011  2641.109149    10090  48678
2775 2011-01-01  2011 2011-06-19       False         0       2011  2649.882774    10090  48678

-- Group ('10093', '48707', 2012, 2012-01-01, 2012-12-31) --
      BeginDate  Year    EndDate  DirectComp  CompRate  SettlYear  ClaimCharge PolicyID  LicNb
2779 2012-01-01  2012 2012-12-31       False         0       2013   293.593099    10093  48707
2780 2012-01-01  2012 2012-12-31       False         0       2014   140.119325    10093  48707
```

**Verdict: (PolicyID, LicNb, Year, BeginDate, EndDate) is NOT unique — 286 groups (604
rows) repeat it with different SettlYear and/or ClaimCharge.** This is consistent with
one row per payment/settlement rather than strictly one row per accident: the same
coverage period produces more than one row, distinguished by SettlYear and a different
ClaimCharge amount.

## Question 3 — Does ClaimNb reconcile at (PolicyID, LicNb, Year)?

Grouped claim rows by (PolicyID, LicNb, Year) (4,244 distinct groups) and compared the
row count per group to ClaimNb on the matching policy row(s) for that key.

A second ambiguity applies here: because policy has multiple sub-period rows per key,
421 of the 79,837 policy (PolicyID, LicNb, Year) groups have **inconsistent ClaimNb
across their own sub-period rows** (e.g., one sub-period shows ClaimNb=0, another
shows ClaimNb=3 for the same key). Which sub-period's ClaimNb to compare against is
therefore not fully determined by the task's join key alone. Both readings are
reported rather than picking one:

**Using the first policy sub-period's ClaimNb per key:**
- Groups compared: 4,244 (all claim groups found a policy key match — 0 unmatched).
- Agree: 4,019. **Disagree: 225.**
- Difference distribution (claim_row_count − ClaimNb): 0 → 4,019; 1 → 217; 2 → 5; 3 → 3.

**Using the max ClaimNb across policy sub-periods per key (sensitivity check):**
- **Disagree: only 6** of 4,244.
- Difference distribution: 0 → 4,238; 1 → 6.

The near-total gap between these two readings (225 vs. 6 disagreements) is explained
by the 421 keys with inconsistent per-sub-period ClaimNb: picking an arbitrary
sub-period's ClaimNb (e.g., "first") frequently picks a sub-period other than the one
the claim actually falls in, producing a spurious mismatch that disappears once the
maximum (or, equivalently, the correct sub-period) is used.

Three example disagreements under the "first" reading, sorted by largest positive gap:

```
     PolicyID  LicNb  Year  claim_row_count  nunique  first  sum  max  diff
1550     1571   8529  2011                3        2      0    3    3     3
2977     5343  28331  2011                3        2      0    3    3     3
2104     2139  12010  2013                4        2      1    4    3     3
```

Full detail for the first example — the claim rows fall in the *second* policy
sub-period (2011-03-27 to 2011-05-24, ClaimNb=3), not the first (ClaimNb=0), which is
exactly the sub-period ClaimNb picked by an arbitrary "first" aggregation:

```
Key: {'PolicyID': '1571', 'LicNb': '8529', 'Year': 2011}
Claim rows:
     BeginDate  Year    EndDate  DirectComp  CompRate  SettlYear  ClaimCharge PolicyID LicNb
496 2011-03-27  2011 2011-05-24       False         0          0   299.639786     1571  8529
497 2011-03-27  2011 2011-05-24       False         0          0  2656.877962     1571  8529
498 2011-03-27  2011 2011-05-24       False         0       2011   163.583922     1571  8529
Policy rows:
     PolicyID LicNb  Year  BeginDate    EndDate  ClaimNb
9379     1571  8529  2011 2011-01-01 2011-03-20        0
9380     1571  8529  2011 2011-03-27 2011-05-24        3
```

Note also: within this same example, claim_row_count = 3 but ClaimNb = 3 already
matches once you pick the right sub-period — yet Q2 found that sub-period's 3 claim
rows are 3 *separate* payment rows (SettlYear 0, 0, 2011) for what may be one accident,
so ClaimNb here is plausibly counting accidents, and claim row count is counting
payments; the two need not be equal even under a "correct" sub-period match in
general. That distinction is not resolved further here.

**Verdict: ClaimNb reconciles almost exactly at (PolicyID, LicNb, Year) once the
correct policy sub-period's ClaimNb is used (6 / 4,244 groups disagree, all off by
exactly 1). Under a naive "first sub-period" reading, 225 / 4,244 groups disagree. This
is a genuinely ambiguous comparison because (PolicyID, LicNb, Year) does not uniquely
pick a policy sub-period** — resolved here by reporting both readings rather than by
picking the more convenient one.

## Context: ClaimCharge sign

| | count | share |
|---|---:|---:|
| Negative | 597 | 13.07% |
| Zero | 2 | 0.04% |
| Positive | 3,969 | 86.89% |
| Missing | 0 | 0.00% |

## Context: negative ClaimCharge by DirectComp

Counts:

| DirectComp | non-negative | negative |
|---|---:|---:|
| False | 3,872 | 570 |
| True | 99 | 27 |

Share within category:

| DirectComp | non-negative | negative |
|---|---:|---:|
| False | 87.17% | 12.83% |
| True | 78.57% | 21.43% |

## Context: negative ClaimCharge by CompRate

Counts:

| CompRate | non-negative | negative |
|---|---:|---:|
| 0 | 3,790 | 570 |
| 50 | 102 | 27 |
| 100 | 79 | 0 |

Share within category:

| CompRate | non-negative | negative |
|---|---:|---:|
| 0 | 86.93% | 13.07% |
| 50 | 79.07% | 20.93% |
| 100 | 100.00% | 0.00% |

## Context: Year range

- Claim table `Year`: 2011 to 2013.
- Policy table `Year`: 2011 to 2013.
- Claim table `SettlYear`: 0 to 2014 (0 appears as a value in this column; not
  investigated further here since it is out of scope for the three questions asked).

## Follow-up: are negatives reserve movements on unsettled claims?

Hypothesis under test: since negatives are mostly not direct-compensation recoveries
(570 of 597 negatives have DirectComp=False, CompRate=0), they might instead be reserve
movements on claims that are not yet settled, with SettlYear=0 taken as the marker for
"unsettled."

- **SettlYear == 0 rows: 41.** SettlYear != 0 rows: 4,527.

Counts (rows = SettlYear==0, columns = ClaimCharge<0):

| SettlYear==0 | ClaimCharge≥0 | ClaimCharge<0 |
|---|---:|---:|
| False (settled) | 3,930 | 597 |
| True (unsettled) | 41 | 0 |

Share within row (`normalize='index'`):

| SettlYear==0 | ClaimCharge≥0 | ClaimCharge<0 |
|---|---:|---:|
| False (settled) | 86.81% | 13.19% |
| True (unsettled) | 100.00% | 0.00% |

Mean / median ClaimCharge, settled vs. unsettled:

| | mean | median |
|---|---:|---:|
| Settled (SettlYear≠0) | 1,400.03 | 1,043.79 |
| Unsettled (SettlYear=0) | 1,587.46 | 1,523.24 |

**Verdict: the hypothesis is not supported — it is refuted by the data.** All 597
negative ClaimCharge rows fall among the 4,527 *settled* rows; none of the 41
SettlYear=0 rows are negative. If SettlYear=0 marks "unsettled," then negatives are not
associated with unsettled claims at all — the reverse pattern holds (unsettled rows are
100% non-negative, with a higher mean/median charge than settled rows). This check does
not identify what the negatives are; it only rules out this specific explanation.

### Reproducible code for this check

```python
import pandas as pd

claim = pd.read_parquet('data/processed/clean_train_claim.parquet')

(claim['SettlYear'] == 0).sum()   # 41
(claim['SettlYear'] != 0).sum()   # 4527

pd.crosstab(claim['SettlYear'] == 0, claim['ClaimCharge'] < 0)
pd.crosstab(claim['SettlYear'] == 0, claim['ClaimCharge'] < 0, normalize='index')

grp = claim.groupby(claim['SettlYear'] == 0)['ClaimCharge']
grp.mean()
grp.median()
```

## Reproducible code

```python
import pandas as pd

claim = pd.read_parquet('data/processed/clean_train_claim.parquet')
policy = pd.read_parquet('data/processed/clean_train_policy.parquet')

join_key = ['PolicyID', 'LicNb', 'Year']
for k in join_key:
    assert k in claim.columns and k in policy.columns

# Preliminary: policy is not unique on (PolicyID, LicNb, Year)
policy_key_dupe_rows = policy.duplicated(subset=join_key, keep=False).sum()
policy_key_groups = policy.groupby(join_key).ngroups
# -> 14270 rows in duplicate-key groups; 79837 distinct key groups (of 87228 rows)

# --- Question 1: BeginDate/EndDate meaning ---
claim_reset = claim.reset_index(drop=True).copy()
claim_reset['_claim_row_id'] = claim_reset.index
merged_full = claim_reset.merge(
    policy[join_key + ['BeginDate', 'EndDate']],
    on=join_key, how='left', suffixes=('_claim', '_policy')
)
no_match = merged_full[merged_full['BeginDate_policy'].isna()]
n_no_match_claims = no_match['_claim_row_id'].nunique()   # 0

cand_per_claim = merged_full.groupby('_claim_row_id').size()
n_single = (cand_per_claim == 1).sum()   # 4117
n_multi = (cand_per_claim > 1).sum()     # 451

merged_full['exact_match'] = (
    (merged_full['BeginDate_claim'] == merged_full['BeginDate_policy']) &
    (merged_full['EndDate_claim'] == merged_full['EndDate_policy'])
)
any_match_per_claim = merged_full.groupby('_claim_row_id')['exact_match'].any()
n_any_match = any_match_per_claim.sum()   # 4568 / 4568 = 100.00%

single_idx = cand_per_claim[cand_per_claim == 1].index
merged_single = merged_full[merged_full['_claim_row_id'].isin(single_idx)]
n_single_match = merged_single['exact_match'].sum()   # 4117 / 4117 = 100.00%

# --- Question 2: uniqueness of (PolicyID, LicNb, Year, BeginDate, EndDate) ---
q2_key = ['PolicyID', 'LicNb', 'Year', 'BeginDate', 'EndDate']
dup_mask = claim.duplicated(subset=q2_key, keep=False)
n_dup_rows = dup_mask.sum()                                    # 604
dup_groups = claim[dup_mask].groupby(q2_key).ngroups            # 286
grp_sizes = claim[dup_mask].groupby(q2_key).size().value_counts().sort_index()
# 2 -> 261 groups, 3 -> 21 groups, 4 -> 3 groups, 7 -> 1 group

# --- Question 3: ClaimNb reconciliation at (PolicyID, LicNb, Year) ---
claim_counts = claim.groupby(join_key).size().reset_index(name='claim_row_count')
policy_claimnb_by_key = policy.groupby(join_key)['ClaimNb'].agg(['nunique', 'first', 'sum', 'max'])
inconsistent_claimnb = (policy_claimnb_by_key['nunique'] > 1).sum()   # 421 of 79837

compare = claim_counts.merge(
    policy_claimnb_by_key.reset_index(), on=join_key, how='left', indicator=True
)
no_policy_key = compare[compare['_merge'] == 'left_only']   # 0 rows

compare = compare[compare['_merge'] == 'both'].copy()
compare['diff'] = compare['claim_row_count'] - compare['first']       # 225 disagree
compare['diff_max'] = compare['claim_row_count'] - compare['max']     # 6 disagree

# --- Context: ClaimCharge sign, cross-tabs, Year range ---
n_neg = (claim['ClaimCharge'] < 0).sum()     # 597
n_zero = (claim['ClaimCharge'] == 0).sum()   # 2
n_pos = (claim['ClaimCharge'] > 0).sum()     # 3969

claim['_neg'] = claim['ClaimCharge'] < 0
pd.crosstab(claim['DirectComp'], claim['_neg'])
pd.crosstab(claim['CompRate'], claim['_neg'])

claim['Year'].min(), claim['Year'].max()     # 2011, 2013
policy['Year'].min(), policy['Year'].max()   # 2011, 2013
```


> Superseded. The CASdatasets documentation states that negative amounts are
> claims where the insured was not at fault, recovered through legal recourse.
> Both hypotheses below were wrong; kept for the record.