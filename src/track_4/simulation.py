"""Fit and simulate the two contracts in spec.qmd using recorded-charge proxies.

Inputs are read-only. Frequency uses ClaimNbClean with supplied Exposure; severity
uses positive ClaimCharge. Individual records are proxies for loss events, and
recorded charges are proxies for ground-up losses, not verified ground-up costs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import tempfile
from time import perf_counter
from typing import Callable

import numpy as np
import pandas as pd
from scipy import optimize, special, stats

from .data_processing import POLICY_KEY_COLUMNS


class SimulationError(ValueError):
    """Inputs or numerical checks do not support a valid simulation."""


@dataclass(frozen=True)
class SimulationSettings:
    years: int = 500_000
    vehicles: int = 1_000
    seed: int = 20260907
    batch_years: int = 10_000
    frequency_grid: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    severity_grid: tuple[float, ...] = (1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3)
    calibration_relative_tolerance: float = 1e-8

    def validate(self) -> None:
        if self.years < 1_000 or self.vehicles < 1 or self.batch_years < 1 or self.seed < 0:
            raise SimulationError("Require >=1,000 years, positive fleet/batch sizes, and a nonnegative seed.")
        if not self.frequency_grid or not self.severity_grid:
            raise SimulationError("Scenario grids cannot be empty.")
        if any(not np.isfinite(x) or not 0 < x <= 1 for x in self.frequency_grid):
            raise SimulationError("Frequency thinning requires multipliers in (0, 1].")
        if any(not np.isfinite(x) or x <= 0 for x in self.severity_grid):
            raise SimulationError("Severity multipliers must be finite and positive.")
        if not 0 < self.calibration_relative_tolerance < 1:
            raise SimulationError("Invalid calibration tolerance.")


@dataclass(frozen=True)
class Contract:
    name: str
    deductible: float
    share: float

    def __post_init__(self) -> None:
        if not np.isfinite(self.deductible) or self.deductible < 0:
            raise SimulationError("Deductible must be finite and nonnegative.")
        if not np.isfinite(self.share) or not 0 <= self.share <= 1:
            raise SimulationError("Insurer share must lie in [0, 1].")


@dataclass(frozen=True)
class FrequencyModel:
    family: str
    rate: float
    theta: float | None = None

    def fleet_moments(self, vehicles: int, multiplier: float = 1.0) -> tuple[float, float]:
        mean = vehicles * self.rate * multiplier
        variance = mean if self.family == "Poisson" else mean + mean**2 / (vehicles * self.theta)
        return mean, variance

    def draw(self, rng: np.random.Generator, years: int, vehicles: int) -> np.ndarray:
        if self.family == "Poisson":
            return rng.poisson(vehicles * self.rate, size=years)
        return rng.negative_binomial(vehicles * self.theta, self.theta / (self.theta + self.rate), size=years)


@dataclass(frozen=True)
class SeverityModel:
    family: str
    shape: float
    scale: float

    def distribution(self, multiplier: float = 1.0):
        family = stats.lognorm if self.family == "Lognormal" else stats.gamma
        return family(self.shape, loc=0, scale=self.scale * multiplier)

    def draw(self, rng: np.random.Generator, size: int) -> np.ndarray:
        if self.family == "Lognormal":
            return rng.lognormal(np.log(self.scale), self.shape, size=size)
        return rng.gamma(self.shape, self.scale, size=size)

    def tail_moment(self, order: int, threshold: float, multiplier: float = 1.0) -> float:
        """E[X**order 1{X > threshold}] under a multiplicative severity shift."""
        scale = self.scale * multiplier
        if self.family == "Lognormal":
            moment = np.exp(order * np.log(scale) + 0.5 * order**2 * self.shape**2)
            z = -np.inf if threshold <= 0 else (
                np.log(threshold / scale) - order * self.shape**2
            ) / self.shape
            return float(moment * stats.norm.sf(z))
        moment = scale**order * np.exp(special.gammaln(self.shape + order) - special.gammaln(self.shape))
        return float(moment * special.gammaincc(self.shape + order, max(threshold, 0) / scale))


@dataclass
class SimulationResult:
    insurer: pd.DataFrame
    retention: pd.DataFrame
    ratios: pd.DataFrame
    checks: pd.DataFrame
    metadata: dict


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_specification(path: Path) -> dict:
    """Read the current spec and extract its contract and central scenario terms."""
    text = path.read_text(encoding="utf-8")
    for heading in ("## Model specification", "## Contract designs", "## Required outputs"):
        if heading not in text:
            raise SimulationError(f"Missing specification section: {heading}")

    def match(pattern: str) -> tuple[float, ...]:
        found = re.search(pattern, text, flags=re.MULTILINE)
        if found is None:
            raise SimulationError(f"Cannot read a required term from {path.name}: {pattern}")
        return tuple(float(value.replace(",", "")) for value in found.groups())

    deductibles = match(r"^\| Deductible[^\n]*?\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|")
    shares = match(r"^\| Insurer share[^\n]*?\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|")
    return {
        "path": path.name, "sha256": _sha256(path),
        "deductibles": deductibles, "shares": shares,
        "vehicles": int(match(r"fleet of \*\*([\d,]+) vehicles\*\*")[0]),
        "years": int(match(r"\*\*([\d,]+) simulations\*\*")[0]),
        "adas_frequency": match(r"Frequency multiplier[^\n]*?=\s*([\d.]+)")[0],
        "adas_severity": match(r"Severity multiplier[^\n]*?=\s*([\d.]+)")[0],
    }


def load_training_data(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    paths = {name: root / "data/processed" / f"clean_train_{name}.parquet" for name in ("policy", "claim")}
    for path in paths.values():
        if not path.is_file():
            raise SimulationError(f"Missing {path}; run the data stage first.")
    policy, claim = (pd.read_parquet(paths[name]) for name in ("policy", "claim"))
    key = list(POLICY_KEY_COLUMNS)
    for frame, required in ((policy, key + ["ClaimNbClean", "Exposure", "Deduc"]),
                            (claim, key + ["ClaimCharge"])):
        missing = set(required) - set(frame.columns)
        if missing or frame.empty:
            raise SimulationError(f"Empty training table or missing columns: {sorted(missing)}")
        if frame[key].isna().any().any():
            raise SimulationError("Incomplete coverage keys cannot be used for simulation.")
    y = policy["ClaimNbClean"].to_numpy(dtype=float, na_value=np.nan)
    exposure = policy["Exposure"].to_numpy(dtype=float, na_value=np.nan)
    amounts = claim["ClaimCharge"].to_numpy(dtype=float, na_value=np.nan)
    if not np.isfinite(y).all() or (y < 0).any() or (y != np.floor(y)).any():
        raise SimulationError("ClaimNbClean must be complete nonnegative integer counts.")
    if not np.isfinite(exposure).all() or (exposure < 0).any() or exposure.sum() <= 0:
        raise SimulationError("Exposure must be finite, nonnegative, and have a positive total.")
    if ((exposure == 0) & (y > 0)).any():
        raise SimulationError("Positive counts with zero exposure cannot be fitted.")
    if not np.isfinite(amounts).all() or (amounts <= 0).any():
        raise SimulationError("Cleaned claims must contain only finite positive charges; no rows are silently dropped.")
    policy_index = pd.MultiIndex.from_frame(policy[key])
    if not policy_index.is_unique or not pd.MultiIndex.from_frame(claim[key]).isin(policy_index).all():
        raise SimulationError("Each cleaned claim must match exactly one policy coverage period.")
    observed = claim.groupby(key).size().reindex(policy_index, fill_value=0).to_numpy()
    if int(y.sum()) != len(claim) or not np.array_equal(y, observed):
        raise SimulationError("Existing ClaimNbClean does not reconcile with cleaned claim rows at every coverage key.")
    details = {
        "policy_rows": len(policy), "claim_rows": len(claim), "positive_counts": int(y.sum()),
        "zero_count_periods": int((y == 0).sum()), "zero_exposure_periods": int((exposure == 0).sum()),
        "total_exposure": float(exposure.sum()), "empirical_annual_rate": float(y.sum() / exposure.sum()),
        "coverage_years": sorted(int(x) for x in policy["Year"].unique()),
        "exposure_mismatch_rows": int(policy.get("ExposureMismatch", pd.Series(dtype=bool)).sum()),
        "charge_mean": float(amounts.mean()), "charge_max": float(amounts.max()),
        "duplicate_record_excess": int(claim.duplicated().sum()),
        "sha256": {str(path.relative_to(root)).replace("\\", "/"): _sha256(path) for path in paths.values()},
    }
    return policy, claim, details


def fit_frequency(policy: pd.DataFrame) -> tuple[FrequencyModel, pd.DataFrame]:
    y = policy["ClaimNbClean"].to_numpy(dtype=float)
    e = policy["Exposure"].to_numpy(dtype=float)
    rate = float(y.sum() / e.sum())
    if rate <= 0:
        raise SimulationError("At least one positive count is needed to fit frequency.")
    mu = rate * e
    pois_ll = float(np.sum(special.xlogy(y, mu) - mu - special.gammaln(y + 1)))
    informative = e > 0
    pois_dispersion = float(np.sum((y[informative] - mu[informative])**2 / mu[informative]) /
                            max(informative.sum() - 1, 1))
    # Repeated (count, exposure) pairs have identical likelihood contributions.
    grouped = pd.DataFrame({"y": y, "e": e}).value_counts().reset_index(name="weight")
    gy, ge, weight = (grouped[name].to_numpy(dtype=float) for name in ("y", "e", "weight"))

    def objective(parameters):
        lam, theta = np.exp(parameters)
        means = lam * ge
        total = theta + means
        ll = (special.gammaln(gy + theta) - special.gammaln(theta) - special.gammaln(gy + 1)
              + theta * (np.log(theta) - np.log(total)) + special.xlogy(gy, means) - gy * np.log(total))
        grad_rate = (gy - means) / (1 + means / theta)
        grad_theta = theta * (special.digamma(gy + theta) - special.digamma(theta)
                             + np.log(theta) + 1 - np.log(total) - (theta + gy) / total)
        return -float(weight @ ll), -np.array([weight @ grad_rate, weight @ grad_theta])

    fits = [optimize.minimize(objective, [np.log(rate), np.log(theta)], jac=True, method="L-BFGS-B",
                             bounds=[(np.log(rate) - 5, np.log(rate) + 5), (-10, 16)],
                             options={"ftol": 1e-13, "gtol": 1e-7, "maxiter": 500})
            for theta in (0.05, 0.5, 5.0)]
    viable = [fit for fit in fits if fit.success and np.isfinite(fit.fun)]
    if not viable:
        raise SimulationError("Negative-binomial fit did not converge; inspect the input/model before proceeding.")
    fitted = min(viable, key=lambda fit: fit.fun)
    nb_rate, theta = np.exp(fitted.x)
    nb_mu = nb_rate * e
    nb_var = nb_mu + nb_mu**2 / theta
    nb_dispersion = float(np.sum((y[informative] - nb_mu[informative])**2 / nb_var[informative]) /
                          max(informative.sum() - 2, 1))
    rows = []
    for family, lam, size, ll, count, dispersion in (
        ("Poisson", rate, None, pois_ll, 1, pois_dispersion),
        ("Negative binomial", float(nb_rate), float(theta), -float(fitted.fun), 2, nb_dispersion),
    ):
        means = lam * e
        pzero = np.exp(-means) if size is None else np.exp(-size * np.log1p(means / size))
        rows.append({"family": family, "annual_rate": lam, "theta": size,
                     "nb2_alpha": None if size is None else 1 / size,
                     "log_likelihood": ll, "aic": 2 * count - 2 * ll,
                     "bic": count * np.log(max(informative.sum(), 1)) - 2 * ll,
                     "pearson_dispersion": dispersion, "expected_training_count": float(means.sum()),
                     "expected_zero_periods": float(pzero.sum()), "converged": True})
    table = pd.DataFrame(rows)
    use_nb = pois_dispersion > 1 and table.loc[1, "aic"] < table.loc[0, "aic"]
    selected = FrequencyModel("Negative binomial", float(nb_rate), float(theta)) if use_nb else FrequencyModel("Poisson", rate)
    table["selected"] = table["family"].eq(selected.family)
    return selected, table


def fit_severity(amounts: np.ndarray) -> tuple[SeverityModel, pd.DataFrame, list[SeverityModel]]:
    if len(amounts) < 3 or not np.isfinite(amounts).all() or (amounts <= 0).any():
        raise SimulationError("Severity fitting requires at least three finite positive charges.")
    candidates, rows = [], []
    for name, family in (("Lognormal", stats.lognorm), ("Gamma", stats.gamma)):
        shape, _, scale = family.fit(amounts, floc=0)
        model = SeverityModel(name, float(shape), float(scale))
        distribution = model.distribution()
        ll = float(distribution.logpdf(amounts).sum())
        cdf = np.clip(distribution.cdf(np.sort(amounts)), 1e-15, 1 - 1e-15)
        n = len(amounts)
        ad = -n - float(np.sum((2 * np.arange(1, n + 1) - 1) * (np.log(cdf) + np.log1p(-cdf[::-1]))) / n)
        rows.append({"family": name, "shape": model.shape, "scale": model.scale, "location": 0.0,
                     "log_likelihood": ll, "aic": 4 - 2 * ll, "bic": 2 * np.log(n) - 2 * ll,
                     "ks_statistic": float(stats.kstest(amounts, distribution.cdf).statistic),
                     "anderson_darling_statistic": ad, "fitted_mean": model.tail_moment(1, 0),
                     "fitted_p95": float(distribution.ppf(.95)), "fitted_p99": float(distribution.ppf(.99))})
        candidates.append(model)
    table = pd.DataFrame(rows)
    selected = candidates[int(table["aic"].argmin())]
    table["selected"] = table["family"].eq(selected.family)
    return selected, table, candidates


def insurer_payment(losses: np.ndarray, contract: Contract) -> np.ndarray:
    return contract.share * np.maximum(losses - contract.deductible, 0)


def contract_moments(model: SeverityModel, contract: Contract, multiplier: float = 1.0) -> dict:
    """First two moments of share * (X - deductible)+ and its zero-payment probability."""
    d, alpha = contract.deductible, contract.share
    below = float(model.distribution(multiplier).cdf(d))
    if alpha == 0:
        return {"mean": 0.0, "second_moment": 0.0, "below_deductible": below}
    low = [model.tail_moment(k, d, multiplier) for k in range(3)]
    mean = alpha * (low[1] - d * low[0])
    second = alpha**2 * (low[2] - 2 * d * low[1] + d**2 * low[0])
    return {"mean": float(max(mean, 0)), "second_moment": float(max(second, 0)),
            "below_deductible": below}


def calibrate_contracts(policy: pd.DataFrame, severity: SeverityModel,
                        spec: dict, settings: SimulationSettings) -> tuple[list[Contract], dict]:
    a = Contract("A", spec["deductibles"][0], spec["shares"][0])
    initial_b = Contract("B", spec["deductibles"][1], spec["shares"][1])
    target = contract_moments(severity, a)["mean"]

    def gap(alpha):
        return contract_moments(severity, replace(initial_b, share=alpha))["mean"] - target

    if target <= 0 or gap(1) < 0:
        raise SimulationError("Equal-cost calibration is not feasible by adjusting B's share in [0, 1].")
    alpha = optimize.brentq(gap, 0, 1, xtol=1e-13, rtol=1e-13)
    b = replace(initial_b, share=float(alpha))
    absolute_gap = gap(alpha)
    if abs(absolute_gap) > settings.calibration_relative_tolerance * target:
        raise SimulationError("Baseline contract calibration exceeded the requested tolerance.")
    return [a, b], {
        "deductible_labels": sorted(str(x) for x in policy["Deduc"].dropna().unique()),
        "initial_terms": [asdict(a), asdict(initial_b)], "final_terms": [asdict(a), asdict(b)],
        "adjusted_term": "Design B insurer share only", "baseline_payment_a": target,
        "initial_payment_b": contract_moments(severity, initial_b)["mean"],
        "baseline_payment_b": contract_moments(severity, b)["mean"],
        "absolute_gap_per_claim": absolute_gap, "relative_gap": absolute_gap / target,
        "relative_tolerance": settings.calibration_relative_tolerance,
    }


def risk_metrics(values: np.ndarray) -> tuple[dict, np.ndarray]:
    """Empirical expected shortfall with quantile-aware Monte Carlo uncertainty.

    TVaR uses exactly the upper 1% probability mass, including fractional weight
    at the quantile when necessary. Its influence score includes uncertainty in
    which years fall in the tail. VaR SE uses a local quantile-spacing density.
    """
    n = len(values)
    if n < 2 or not np.isfinite(values).all():
        raise SimulationError("Tail summaries require at least two finite simulated years.")
    sd = float(np.std(values, ddof=1))
    result = {"mean": float(np.mean(values)), "mean_mcse": sd / np.sqrt(n), "sd": sd}
    for level, label in ((.95, "var95"), (.99, "var99")):
        bandwidth = min((1 - level) / 2, level / 2, max(1 / n, np.sqrt(level * (1 - level)) * n**(-1 / 3)))
        low, quantile, high = np.quantile(values, [level - bandwidth, level, level + bandwidth], method="inverted_cdf")
        se = np.sqrt(level * (1 - level) / n) * (high - low) / (2 * bandwidth)
        result[label], result[label + "_mcse"] = float(quantile), float(se)
    score = result["var99"] + np.maximum(values - result["var99"], 0) / .01
    result["tvar99"] = float(score.mean())
    result["tvar99_mcse"] = float(score.std(ddof=1) / np.sqrt(n))
    return result, score - result["tvar99"]


def paired_tvar_ratio(a: dict, b: dict, influence_a: np.ndarray, influence_b: np.ndarray) -> dict:
    if b["tvar99"] <= 0:
        raise SimulationError("A/B TVaR ratio is undefined when B's tail cost is zero.")
    ratio = a["tvar99"] / b["tvar99"]
    se = float(np.std((influence_a - ratio * influence_b) / b["tvar99"], ddof=1) / np.sqrt(len(influence_a)))
    return {"tvar99_ratio_a_b": ratio, "ratio_mcse": se,
            "ratio_ci95_low": ratio - 1.96 * se, "ratio_ci95_high": ratio + 1.96 * se}


def _scenario_name(phi_n: float, phi_x: float, spec: dict) -> str:
    if np.isclose(phi_n, 1) and np.isclose(phi_x, 1):
        return "Baseline"
    if np.isclose(phi_n, spec["adas_frequency"]) and np.isclose(phi_x, spec["adas_severity"]):
        return "ADAS"
    return "Sensitivity"


def _simulate_frequency_slice(frequency: FrequencyModel, severity: SeverityModel,
                              contracts: list[Contract], phi_n: float, settings: SimulationSettings):
    """Bound memory by simulating year batches, preserving empty fleet years.

    Replay the same independent count/severity/mark streams at each frequency.
    Thinning baseline claims gives the correct Poisson or NB2 marginals while
    keeping both designs and all severity shifts on common claims.
    """
    count_rng, severity_rng, mark_rng = [np.random.default_rng(np.random.SeedSequence(settings.seed, spawn_key=(i,)))
                                        for i in range(3)]
    annual = np.empty((len(settings.severity_grid), len(contracts), settings.years), dtype=np.float64)
    ground = np.empty(settings.years, dtype=np.float64)
    count = np.empty(settings.years, dtype=np.int64)
    below = np.zeros((len(settings.severity_grid), len(contracts)), dtype=np.int64)
    max_relative_balance_error = 0.0
    for start in range(0, settings.years, settings.batch_years):
        stop = min(start + settings.batch_years, settings.years)
        batch_size = stop - start
        base_counts = frequency.draw(count_rng, batch_size, settings.vehicles)
        total = int(base_counts.sum())
        base_amounts = severity.draw(severity_rng, total)
        keep = mark_rng.random(total) < phi_n
        year_index = np.repeat(np.arange(batch_size, dtype=np.int32), base_counts)[keep]
        base_amounts = base_amounts[keep]
        count[start:stop] = np.bincount(year_index, minlength=batch_size)
        ground[start:stop] = np.bincount(year_index, weights=base_amounts, minlength=batch_size)
        for j, phi_x in enumerate(settings.severity_grid):
            losses = base_amounts * phi_x
            aggregate_loss = ground[start:stop] * phi_x
            for k, contract in enumerate(contracts):
                excess = np.maximum(losses - contract.deductible, 0)
                payment = insurer_payment(losses, contract)
                # Independently express retention to check the payment transformation.
                retention = np.minimum(losses, contract.deductible) + (1 - contract.share) * excess
                if (payment < 0).any() or (payment > losses + 1e-9).any():
                    raise SimulationError("Insurer payment is outside [0, loss].")
                payment_total = np.bincount(year_index, weights=payment, minlength=batch_size)
                retention_total = np.bincount(year_index, weights=retention, minlength=batch_size)
                annual[j, k, start:stop] = payment_total
                residual = np.abs(aggregate_loss - payment_total - retention_total) / (1 + aggregate_loss)
                max_relative_balance_error = max(max_relative_balance_error, float(residual.max(initial=0)))
                below[j, k] += np.count_nonzero(losses <= contract.deductible)
    if max_relative_balance_error > 1e-10:
        raise SimulationError(f"Loss conservation failed: relative error {max_relative_balance_error:g}")
    return annual, ground, count, below, max_relative_balance_error


def simulate_scenarios(frequency: FrequencyModel, severity: SeverityModel, contracts: list[Contract],
                       settings: SimulationSettings, spec: dict, progress: Callable[[str], None] = print):
    settings.validate()
    insurer_rows, retention_rows, ratio_rows, checks = [], [], [], []
    distributions = {}
    for phi_n in settings.frequency_grid:
        tick = perf_counter()
        progress(f"Simulating frequency x{phi_n:.2f}: {settings.years:,} years, all severity shifts and both designs...")
        annual, ground, counts, below, balance = _simulate_frequency_slice(
            frequency, severity, contracts, phi_n, settings
        )
        mean_n, var_n = frequency.fleet_moments(settings.vehicles, phi_n)
        count_se = np.sqrt(var_n / settings.years)
        count_z = (counts.mean() - mean_n) / count_se
        checks.append({"check": "Annual count mean", "frequency_multiplier": phi_n,
                       "status": "PASS" if abs(count_z) <= 6 else "WARN", "standardized_error": count_z})
        checks.append({"check": "Payments and retention balance", "frequency_multiplier": phi_n,
                       "status": "PASS", "max_relative_error": balance})
        total_claims = int(counts.sum())
        for j, phi_x in enumerate(settings.severity_grid):
            scenario = _scenario_name(phi_n, phi_x, spec)
            loss_mean = severity.tail_moment(1, 0, phi_x)
            loss_second = severity.tail_moment(2, 0, phi_x)
            annual_losses = ground * phi_x
            ground_expectation = mean_n * loss_mean
            ground_variance = mean_n * loss_second + (var_n - mean_n) * loss_mean**2
            ground_z = (annual_losses.mean() - ground_expectation) / np.sqrt(ground_variance / settings.years)
            checks.append({"check": "Proxy ground-up mean", "frequency_multiplier": phi_n,
                           "severity_multiplier": phi_x, "status": "PASS" if abs(ground_z) <= 6 else "WARN",
                           "standardized_error": ground_z})
            key = {"scenario": scenario, "frequency_multiplier": phi_n, "severity_multiplier": phi_x}
            metrics, influences = [], []
            for k, contract in enumerate(contracts):
                values = annual[j, k]
                risk, influence = risk_metrics(values)
                moments = contract_moments(severity, contract, phi_x)
                expected_cost = mean_n * moments["mean"]
                expected_variance = mean_n * moments["second_moment"] + (var_n - mean_n) * moments["mean"]**2
                z = (risk["mean"] - expected_cost) / np.sqrt(expected_variance / settings.years)
                insurer_rows.append({**key, "design": contract.name, **risk,
                                     "model_mean": expected_cost, "model_sd": np.sqrt(expected_variance),
                                     "mean_check_z": z, "simulated_claims": total_claims})
                checks.append({**key, "design": contract.name, "check": "Insurer mean vs model",
                               "status": "PASS" if abs(z) <= 6 else "WARN", "standardized_error": z})
                retained = annual_losses - values
                retention_rows.append({**key, "design": contract.name,
                                       "expected_retention_per_claim": loss_mean - moments["mean"],
                                       "expected_retention_annual_per_vehicle": (mean_n / settings.vehicles) * (loss_mean - moments["mean"]),
                                       "retention_share_of_losses": (loss_mean - moments["mean"]) / loss_mean,
                                       "mc_retention_per_claim": float(retained.sum() / total_claims),
                                       "mc_retention_annual_per_vehicle": float(retained.mean() / settings.vehicles),
                                       "mc_retention_share": float(retained.sum() / annual_losses.sum()),
                                       "expected_below_deductible": moments["below_deductible"],
                                       "mc_below_deductible": float(below[j, k] / total_claims)})
                if scenario in ("Baseline", "ADAS"):
                    distributions[(scenario, contract.name)] = values.copy()
                metrics.append(risk)
                influences.append(influence)
            ratio = paired_tvar_ratio(*metrics, *influences)
            delta = annual[j, 0] - annual[j, 1]
            ratio_rows.append({**key, **ratio, "mean_a_minus_b": float(delta.mean()),
                               "mean_difference_mcse": float(delta.std(ddof=1) / np.sqrt(settings.years))})
        progress(f"Finished frequency x{phi_n:.2f} in {perf_counter() - tick:.1f}s; {total_claims:,} shared claims.")
    return (pd.DataFrame(insurer_rows), pd.DataFrame(retention_rows), pd.DataFrame(ratio_rows),
            pd.DataFrame(checks), distributions)


def _number(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (bool, np.bool_)):
        return "Yes" if value else "No"
    if isinstance(value, (float, np.floating)):
        if value != 0 and abs(value) < .0001:
            return f"{value:.3e}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if isinstance(value, (int, np.integer)):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _markdown_table(frame: pd.DataFrame, columns: dict[str, str] | None = None) -> list[str]:
    if columns:
        frame = frame[list(columns)].rename(columns=columns)
    return ["| " + " | ".join(frame.columns) + " |", "|" + "---|" * len(frame.columns)] + [
        "| " + " | ".join(_number(value) for value in row) + " |" for row in frame.itertuples(index=False, name=None)
    ]


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _save_plots(graphs: Path, policy: pd.DataFrame, amounts: np.ndarray, frequency_table: pd.DataFrame,
                candidates: list[SeverityModel], result: SimulationResult, distributions: dict) -> list[str]:
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "mfi-track4-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    graphs.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                         "figure.facecolor": "white", "axes.grid": True, "grid.alpha": .2})
    names = []

    def save(fig, name):
        fig.savefig(graphs / f"{name}.png", dpi=180, bbox_inches="tight")
        plt.close(fig)
        names.append(name)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), layout="constrained")
    probabilities = np.linspace(.002, .998, 250)
    observed = np.quantile(amounts, probabilities)
    for ax, candidate in zip(axes, candidates):
        theoretical = candidate.distribution().ppf(probabilities)
        ax.scatter(theoretical, observed, s=13, color="#315a88", alpha=.8)
        edge = max(theoretical.max(), observed.max())
        ax.plot([0, edge], [0, edge], "--", color="#777777", linewidth=1)
        selected = " (selected)" if candidate.family == result.metadata["severity"]["family"] else ""
        ax.set(title=candidate.family + selected, xlabel="Fitted quantiles (€)", ylabel="Recorded-charge quantiles (€)")
    fig.suptitle("Severity fit: recorded charges used as ground-up loss proxies")
    save(fig, "severity_qq")

    fig, ax = plt.subplots(figsize=(9, 4.8), layout="constrained")
    y = policy["ClaimNbClean"].to_numpy(dtype=int)
    e = policy["Exposure"].to_numpy(dtype=float)
    labels = ["0", "1", "2", "3", "4", "5+"]
    x = np.arange(6)
    observed_counts = np.array([(y == k).sum() for k in range(5)] + [(y >= 5).sum()])
    ax.bar(x - .25, observed_counts, width=.25, color="#444444", label="Observed")
    for index, row in frequency_table.iterrows():
        mu = row["annual_rate"] * e
        distribution = stats.poisson(mu) if row["family"] == "Poisson" else stats.nbinom(row["theta"], row["theta"] / (row["theta"] + mu))
        predicted = [distribution.pmf(k).sum() for k in range(5)] + [distribution.sf(4).sum()]
        ax.bar(x + index * .25, predicted, width=.25, color=("#315a88", "#bd6438")[index], label=row["family"])
    ax.set(xticks=x, xticklabels=labels, xlabel="Positive claim records per policy period",
           ylabel="Number of periods (log scale)", yscale="log", title="Frequency fits account for each period's supplied exposure")
    ax.legend()
    save(fig, "frequency_fit")

    save(_plot_insurer_comparison(result), "insurer_comparison")

    fig, ax = plt.subplots(figsize=(8, 5), layout="constrained")
    exceedance = np.geomspace(1e-5, .999, 500)
    for scenario, style in (("Baseline", "-"), ("ADAS", "--")):
        for k, design in enumerate(("A", "B")):
            values = distributions[(scenario, design)]
            ax.plot(np.quantile(values, 1 - exceedance) / 1000, exceedance, style,
                    color=("#315a88", "#bd6438")[k], label=f"{scenario}, {design}")
    ax.set(title="Annual costs from shared simulated proxy losses", xlabel="Annual insurer cost (€ thousands)",
           ylabel="Probability annual cost exceeds x", yscale="log", ylim=(1e-5, 1))
    ax.axhline(.01, color="#777777", linewidth=.8, linestyle=":")
    ax.legend(fontsize=9)
    save(fig, "aggregate_survival")

    save(_plot_sensitivity_contour(result), "sensitivity_contour")
    return names


def _plot_insurer_comparison(result: SimulationResult):
    from matplotlib import pyplot as plt

    main = result.insurer.loc[result.insurer["scenario"].isin(["Baseline", "ADAS"])]
    fig, ax = plt.subplots(figsize=(8, 5.2), layout="constrained")
    rows = main.set_index(["scenario", "design"])
    for k, design in enumerate(("A", "B")):
        positions = np.array([0, 1]) + (k - .5) * .28
        selected = rows.loc[[("Baseline", design), ("ADAS", design)]]
        ax.bar(positions, selected["tvar99"] / 1000, width=.27, color=("#315a88", "#bd6438")[k],
               label=f"Design {design}: TVaR99", yerr=1.96 * selected["tvar99_mcse"] / 1000, capsize=4)
        ax.scatter(positions, selected["mean"] / 1000, color="white", edgecolor="black", s=45, zorder=3,
                   label="Mean" if k == 0 else None)
    ax.set(xticks=[0, 1], xticklabels=["Baseline", "ADAS"], ylabel="Annual insurer cost (€ thousands)",
           title=f"{result.metadata['settings']['vehicles']:,} vehicles: costs under the proxy-loss model\n"
                 "TVaR99 with 95% Monte Carlo error bars")
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3, frameon=False, fontsize=10)
    return fig


def _plot_sensitivity_contour(result: SimulationResult):
    from matplotlib import pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 5.2), layout="constrained")
    ratios = result.ratios
    low, high = ratios["tvar99_ratio_a_b"].min(), ratios["tvar99_ratio_a_b"].max()
    if high - low < 1e-8:
        low, high = low - .001, high + .001
    levels = np.linspace(low, high, 13)
    grid = ratios.pivot(index="frequency_multiplier", columns="severity_multiplier", values="tvar99_ratio_a_b")
    contour = ax.contourf(grid.columns, grid.index, grid.to_numpy(), levels=levels, cmap="viridis")
    if grid.to_numpy().min() < 1 < grid.to_numpy().max():
        ax.contour(grid.columns, grid.index, grid.to_numpy(), levels=[1], colors="white", linewidths=1.5)
    ax.scatter([1, result.metadata["spec"]["adas_severity"]], [1, result.metadata["spec"]["adas_frequency"]],
               marker="X", color="white", edgecolor="black", s=80, zorder=3, clip_on=False)
    ax.annotate("Baseline", (1, 1), xytext=(7, -15), textcoords="offset points")
    ax.annotate("ADAS", (result.metadata["spec"]["adas_severity"], result.metadata["spec"]["adas_frequency"]),
                xytext=(7, 5), textcoords="offset points")
    ax.set(xlabel="Severity multiplier", ylabel="Frequency multiplier",
           title="Sensitivity under the proxy-loss model\nCalibrated deductibles and shares held fixed")
    fig.colorbar(contour, ax=ax, label="TVaR99(A) / TVaR99(B); values above 1 favor B", shrink=.85)
    return fig


def _write_simulation_report(path: Path, result: SimulationResult) -> None:
    meta = result.metadata
    data, config, cal = meta["data"], meta["settings"], meta["calibration"]
    main_cost = result.insurer.loc[result.insurer["scenario"].isin(["Baseline", "ADAS"])].sort_values(["scenario", "design"])
    main_retention = result.retention.loc[result.retention["scenario"].isin(["Baseline", "ADAS"])].sort_values(["scenario", "design"])
    main_ratios = result.ratios.loc[result.ratios["scenario"].isin(["Baseline", "ADAS"])].sort_values("scenario")
    warnings = int(result.checks["status"].eq("WARN").sum())
    lines = [
        "# Contract simulation", "",
        "**Recorded ClaimCharge values are proxies for ground-up losses, not verified ground-up repair costs.** "
        "All euro amounts are on the recorded historical-charge scale. Results are conditional on the fitted models and assumptions below.", "",
        f"Completed {config['years']:,} simulated years of {config['vehicles']:,} vehicles at every scenario/grid point; "
        f"seed {config['seed']}. Numerical checks: 0 failures, {warnings} warnings. "
        "Cleaned input files were not modified.", "",
        "Run: `uv run src/run_pipeline.py --stage simulation`. The default command reuses existing cleaned files; "
        "`--stage processing` explicitly refreshes them.", "",
        "## Main finding", "",
    ]
    for _, row in main_ratios.loc[main_ratios["scenario"].eq("ADAS")].iterrows():
        verdict = "B has lower TVaR99" if row["ratio_ci95_low"] > 1 else "A has lower TVaR99" if row["ratio_ci95_high"] < 1 else "the A/B tail ranking is unresolved at this Monte Carlo precision"
        lines.append(f"ADAS: {verdict}; A/B TVaR99 ratio {_number(row['tvar99_ratio_a_b'])}, "
                     f"MC SE {_number(row['ratio_mcse'])}, 95% MC interval "
                     f"[{_number(row['ratio_ci95_low'])}, {_number(row['ratio_ci95_high'])}].")
    if result.ratios["ratio_ci95_low"].gt(1).all():
        lines += ["", f"B has lower TVaR99 at every tested grid point; ratios range from "
                  f"{result.ratios['tvar99_ratio_a_b'].min():.4f} to {result.ratios['tvar99_ratio_a_b'].max():.4f}. "
                  "This is an insurer-tail ranking under the proxy-loss model, not an overall welfare ranking."]
    adas_retention = main_retention.loc[main_retention["scenario"].eq("ADAS")].set_index("design")
    lines += ["", "Policyholder tradeoff in the ADAS scenario: expected annual retention per vehicle is "
              f"€{adas_retention.loc['A', 'expected_retention_annual_per_vehicle']:.2f} for A and "
              f"€{adas_retention.loc['B', 'expected_retention_annual_per_vehicle']:.2f} for B. "
              "The lower insurer cost comes with greater expected policyholder retention under B."]
    lines += ["", "## Training data and model fits", "",
              f"All {data['policy_rows']:,} coverage periods are retained, including {data['zero_count_periods']:,} zero-count periods. "
              f"Supplied Exposure totals {_number(data['total_exposure'])}; {data['zero_exposure_periods']:,} periods have zero exposure. "
              f"Existing ClaimNbClean sums to {data['positive_counts']:,}, exactly matching {data['claim_rows']:,} "
              "positive cleaned claim rows both overall and at (PolicyID, LicNb, Year, BeginDate, EndDate). "
              "No counts are rebuilt from the older two-field join in the spec's R example.", "",
              f"Frequency: intercept-only Poisson and NB2 maximum likelihood, mean per period = annual rate × supplied Exposure. "
              "Zero-exposure/zero-count periods, if present, contribute their exact probability-one outcome. "
              "NB2 is selected when its AIC is lower and Poisson's Pearson dispersion exceeds one. "
              "NB2 variance = mean + mean²/theta; alpha = 1/theta. "
              "The varying-exposure NB2 rate need not equal the raw count/exposure ratio. "
              "See the [NB2 model definition](https://www.statsmodels.org/stable/generated/statsmodels.discrete.discrete_model.NegativeBinomial.html).", ""]
    lines += _markdown_table(pd.DataFrame(meta["frequency_candidates"]), {
        "family": "Model", "annual_rate": "Annual rate", "theta": "Theta", "nb2_alpha": "Alpha",
        "log_likelihood": "Log likelihood", "aic": "AIC", "bic": "BIC", "pearson_dispersion": "Pearson/df",
        "expected_training_count": "Fitted total count", "converged": "Converged", "selected": "Selected"})
    lines += ["", "Severity: maximum likelihood with location fixed at zero; lower AIC selects the model, with QQ and "
              "distribution-distance diagnostics reported alongside. KS and Anderson–Darling values are descriptive; "
              "no ordinary fitted-parameter KS p-value is claimed. Gamma uses shape and scale (rate = 1/scale); "
              "lognormal uses sigma = shape and mu_log = log(scale). "
              "Parameter conventions follow [SciPy lognormal](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.lognorm.html) "
              "and [gamma](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.gamma.html).", ""]
    lines += _markdown_table(pd.DataFrame(meta["severity_candidates"]), {
        "family": "Model", "shape": "Shape", "scale": "Scale", "log_likelihood": "Log likelihood",
        "aic": "AIC", "bic": "BIC", "ks_statistic": "KS", "anderson_darling_statistic": "AD",
        "fitted_mean": "Fitted mean", "fitted_p99": "Fitted q99", "selected": "Selected"})
    lines += ["", "![Frequency model diagnostics](graphs/frequency_fit.png)", "",
              "![Severity QQ diagnostics](graphs/severity_qq.png)", "",
              "## Contracts and calibration", "",
              "Payment is `share * max(proxy_loss - deductible, 0)`. "
              "Calibrate B's share only against A using analytic severity partial moments under the unshifted selected model. "
              "Deductibles and shares are then fixed for every grid point.", ""]
    terms = pd.DataFrame([{"stage": stage, **term} for stage, group in (("Initial", cal["initial_terms"]), ("Calibrated", cal["final_terms"])) for term in group])
    lines += _markdown_table(terms, {"stage": "Stage", "name": "Design", "deductible": "Deductible (€)", "share": "Insurer share"})
    annual_count = meta["baseline_fleet_mean_count"]
    lines += ["", f"Calibrated expected payment per claim: A = €{_number(cal['baseline_payment_a'])}; B = €{_number(cal['baseline_payment_b'])}. "
              f"Expected baseline annual fleet cost = €{_number(annual_count * cal['baseline_payment_a'])}. "
              f"A/B calibration gap (B minus A): €{_number(cal['absolute_gap_per_claim'])} per claim, "
              f"relative gap {_number(cal['relative_gap'])}; required relative tolerance {cal['relative_tolerance']:.1e}.", "",
              "Observed Deduc labels: " + ", ".join(cal["deductible_labels"]) + ". "
              "The starting €250 deductible falls in 201–300 euros; €1,000 is compatible with the open >600 band, "
              "which does not establish an exact sold deductible.", "",
              "## Annual insurer results", "",
              f"Amounts are euros per {config['vehicles']:,}-vehicle fleet year. MC SE is shown for each tail measure. "
              "Mean MC SE, analytic mean/SD, and all sensitivity rows are also exported to `simulation_insurer.csv`.", ""]
    risk_columns = {"scenario": "Scenario", "design": "Design", "mean": "Mean", "sd": "SD",
                    "var95": "VaR95", "var95_mcse": "SE", "var99": "VaR99", "var99_mcse": "SE ",
                    "tvar99": "TVaR99", "tvar99_mcse": "SE  "}
    lines += _markdown_table(main_cost, risk_columns)
    lines += ["", "A/B ratios and mean differences use paired years. A ratio above one favors B on TVaR99.", ""]
    lines += _markdown_table(main_ratios, {"scenario": "Scenario", "tvar99_ratio_a_b": "TVaR A/B",
                                          "ratio_mcse": "Ratio MC SE", "ratio_ci95_low": "95% lower", "ratio_ci95_high": "95% upper",
                                          "mean_a_minus_b": "Mean A−B (€)", "mean_difference_mcse": "Paired mean SE (€)"})
    lines += ["", "![Mean and tail comparison](graphs/insurer_comparison.png)", "",
              "![Annual insurer cost distributions](graphs/aggregate_survival.png)", "",
              "## Retention and contract diagnostics", "",
              "Expected retention comes from model moments. Annual retention per vehicle includes zero-claim years. "
              "Retention share = expected retained loss / expected total proxy loss. Threshold probabilities are "
              "per claim, separately for A and B; model and simulated probabilities are both shown.", ""]
    retention_columns = {"scenario": "Scenario", "design": "Design",
                         "expected_retention_per_claim": "Retention/claim (€)",
                         "expected_retention_annual_per_vehicle": "Retention/vehicle/year (€)",
                         "retention_share_of_losses": "Retention share", "expected_below_deductible": "Below d (model)",
                         "mc_below_deductible": "Below d (MC)"}
    lines += _markdown_table(main_retention, retention_columns)
    lines += ["", "Below-deductible proxy losses generate zero insurer payment and could go unreported in future insurer data. "
              "Above-deductible payments grow with loss at the insurer's share. For a positive payment, the proxy loss "
              "can be recovered as deductible + payment/share when the terms are known. "
              "Truncation or censoring already present in the recorded training charges is not repaired here.", "",
              "## Sensitivity", "",
              f"Frequency grid: {config['frequency_grid']}; severity grid: {config['severity_grid']}. "
              f"Each cell uses {config['years']:,} simulated years for both designs. "
              "The contour interpolates between simulated cells; it does not add simulation points.", "",
              "![Sensitivity contour](graphs/sensitivity_contour.png)", "",
              "<details>", "<summary>All grid ratios and Monte Carlo uncertainty</summary>", ""]
    lines += _markdown_table(result.ratios, {"frequency_multiplier": "Frequency ×", "severity_multiplier": "Severity ×",
                                            "tvar99_ratio_a_b": "TVaR A/B", "ratio_mcse": "MC SE", "ratio_ci95_low": "95% lower", "ratio_ci95_high": "95% upper"})
    lines += ["", "</details>", "", "<details>", "<summary>Full insurer summary at every scenario/grid point</summary>", ""]
    grid_risk_columns = {"frequency_multiplier": "Frequency ×", "severity_multiplier": "Severity ×",
                         **{k: v for k, v in risk_columns.items() if k != "scenario"}}
    lines += _markdown_table(result.insurer, grid_risk_columns)
    lines += ["", "</details>", "", "<details>", "<summary>Full retention and threshold summary at every scenario/grid point</summary>", ""]
    grid_retention_columns = {"frequency_multiplier": "Frequency ×", "severity_multiplier": "Severity ×",
                              **{k: v for k, v in retention_columns.items() if k != "scenario"}}
    lines += _markdown_table(result.retention, grid_retention_columns)
    lines += ["", "</details>", "", "## Simulation settings, assumptions, and numerical checks", "",
              f"Selected models: {meta['frequency']['family']} frequency; {meta['severity']['family']} severity. "
              f"Baseline fleet count mean = {_number(meta['baseline_fleet_mean_count'])}, variance = "
              f"{_number(meta['baseline_fleet_count_variance'])}. NB fleet shape is vehicles × per-vehicle theta, "
              "and p = theta / (theta + rate). Under frequency multiplier phi, rate becomes phi × rate and theta "
              "is held fixed; fleet shape still scales with vehicle count. "
              "This follows the [negative-binomial parameterization](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.nbinom.html).", "",
              f"A PCG64 generator uses fixed seed {config['seed']} with independent count, severity, and thinning-mark streams. "
              f"Batches contain at most {config['batch_years']:,} years. Baseline claims are thinned by frequency multiplier "
              "and severity amounts multiplied by the severity factor (constant severity CV). Both contracts use exactly "
              "the same claims. Common streams are replayed across frequency settings; grid estimates are correlated.", "",
              "VaR uses the empirical inverse CDF. TVaR99 is `q99 + mean(max(S-q99,0))/0.01`, so it weights exactly the worst "
              "1% of probability mass, including ties. TVaR MC SE is the sample SD of its influence score divided by sqrt(years), "
              "which accounts for the estimated tail threshold. VaR MC SE uses local quantile spacings to estimate density. "
              "Ratio MC SE uses the paired influence scores and the delta method. Intervals are approximate normal 95% MC intervals; "
              "parameter estimation and model uncertainty are not included.", "",
              "- Retained positive records are treated as independent loss-event proxies. The current claim checks do not "
              "establish accident-versus-payment granularity, despite stronger wording in the older spec. Duplicates remain counted.",
              "- The project carries the legal-recourse interpretation of nonpositive charges from PG17 to PG16. The 597 negative "
              "and two zero raw charges were excluded in processing; this does not verify fault status for every positive record.",
              f"- Years {data['coverage_years']} are pooled without inflation adjustment or covariates. All supplied exposure is retained; "
              f"{data['exposure_mismatch_rows']:,} periods have the previously documented date/exposure discrepancy. No extra VehiclNb multiplier is applied.",
              "- Independent vehicles, independent severities, and count/severity independence are assumed. Repeat vehicle periods, "
              "common shocks, behavior changes, expenses, and recovery cash flows are not modeled.",
              "- ADAS frequency ×0.60 and severity ×1.15 are specified scenario assumptions, not causal estimates fitted from these data. "
              "Uniform severity scaling does not model a change in the claim mix.", "",
              "Checks compare sampled annual count, proxy-loss, and insurer means with analytic compound-model expectations "
              "using a six-MC-SE tolerance. Every simulated payment is checked to be between zero and the loss; an independent "
              "retention expression checks loss = payment + retention. Full results are in `simulation_checks.csv`.", ""]
    grouped = result.checks.groupby(["check", "status"]).size().reset_index(name="cases")
    lines += _markdown_table(grouped, {"check": "Check", "status": "Status", "cases": "Cases"})
    bad = result.checks.loc[result.checks["status"].ne("PASS")]
    if not bad.empty:
        lines += [""] + _markdown_table(bad.fillna(""))
    lines += ["", f"Maximum relative loss-balance error: {_number(result.checks['max_relative_error'].max())}. "
              f"Runtime: {meta['elapsed_seconds']:.1f} seconds. Spec SHA-256: `{meta['spec']['sha256']}`.", "",
              "Files: `simulation_metadata.json` (parameters, calibration, settings, input hashes, versions); "
              "`simulation_insurer.csv`, `simulation_retention.csv`, `simulation_ratios.csv`, and `simulation_checks.csv`. "
              "Graphs are exported as PNG in `results/graphs`.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_simulation(project_root: Path | str, settings: SimulationSettings | None = None,
                   progress: Callable[[str], None] = print) -> SimulationResult:
    root = Path(project_root).resolve()
    started = perf_counter()
    spec = read_specification(root / "spec.qmd")
    settings = settings or SimulationSettings(years=spec["years"], vehicles=spec["vehicles"])
    settings = replace(settings,
                       frequency_grid=tuple(sorted(set(settings.frequency_grid) | {1., spec["adas_frequency"]})),
                       severity_grid=tuple(sorted(set(settings.severity_grid) | {1., spec["adas_severity"]})))
    settings.validate()
    if len(settings.frequency_grid) < 2 or len(settings.severity_grid) < 2:
        raise SimulationError("Contour output requires at least two frequency and severity grid values.")
    progress("Checking cleaned training data and fitting exposure-adjusted frequency and positive-charge severity...")
    policy, claim, data = load_training_data(root)
    amounts = claim["ClaimCharge"].to_numpy(dtype=float)
    frequency, frequency_table = fit_frequency(policy)
    severity, severity_table, candidates = fit_severity(amounts)
    contracts, calibration = calibrate_contracts(policy, severity, spec, settings)
    progress(f"Selected {frequency.family}, annual rate {frequency.rate:.8f}; {severity.family} severity. "
             f"Calibrated B share {contracts[1].share:.8f}.")
    insurer, retention, ratios, checks, distributions = simulate_scenarios(
        frequency, severity, contracts, settings, spec, progress
    )
    for relative, digest in data["sha256"].items():
        if _sha256(root / relative) != digest:
            raise SimulationError(f"Cleaned input changed during simulation: {relative}")
    count_mean, count_variance = frequency.fleet_moments(settings.vehicles)
    metadata = {
        "spec": spec, "settings": asdict(settings), "data": data, "frequency": asdict(frequency),
        "severity": asdict(severity), "frequency_candidates": frequency_table.to_dict("records"),
        "severity_candidates": severity_table.to_dict("records"), "calibration": calibration,
        "baseline_fleet_mean_count": count_mean, "baseline_fleet_count_variance": count_variance,
        "ground_up_proxy": True, "monte_carlo_uncertainty_only": True,
        "versions": {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "pandas", "pyarrow", "matplotlib")},
    }
    result = SimulationResult(insurer, retention, ratios, checks, metadata)
    output = root / "results"
    output.mkdir(parents=True, exist_ok=True)
    metadata["graphs"] = _save_plots(output / "graphs", policy, amounts, frequency_table, candidates, result, distributions)
    for name, frame in (("insurer", insurer), ("retention", retention), ("ratios", ratios), ("checks", checks)):
        frame.to_csv(output / f"simulation_{name}.csv", index=False)
    metadata["elapsed_seconds"] = perf_counter() - started
    (output / "simulation_metadata.json").write_text(json.dumps(_json_safe(metadata), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _write_simulation_report(output / "simulation.md", result)
    progress(f"Simulation complete. Report: {output / 'simulation.md'}")
    return result
