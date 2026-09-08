"""Core contract, count-scaling, tail-estimator and simulation regression checks."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
from scipy import integrate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from track_4.simulation import (
    Contract, FrequencyModel, SeverityModel, SimulationSettings, _simulate_frequency_slice,
    calibrate_contracts, contract_moments, fit_frequency, fit_severity, insurer_payment,
    load_training_data, paired_tvar_ratio, read_specification, risk_metrics,
)


class SimulationTests(unittest.TestCase):
    def test_payment_and_limit_apply_to_insurer_share(self):
        contract = Contract("B", 250, .8, 1000)
        np.testing.assert_array_equal(insurer_payment(np.array([0, 250, 500, 1500, 3000]), contract),
                                      [0, 0, 200, 1000, 1000])
        self.assertEqual(insurer_payment(np.array([100]), Contract("A", 0, 0, 10))[0], 0)
        for args in (("A", -1, 1, 100), ("A", 0, 1.1, 100), ("A", 0, 1, 0)):
            with self.assertRaises(ValueError):
                Contract(*args)

    def test_closed_form_payment_moments_against_integration(self):
        for severity in (SeverityModel("Gamma", 1.4, 1200), SeverityModel("Lognormal", .8, 900)):
            for limit in (700., float("inf")):
                contract = Contract("B", 250, .63, limit)
                moments = contract_moments(severity, contract, 1.15)
                dist = severity.distribution(1.15)
                cap_point = contract.deductible + limit / contract.share
                for order, field in ((1, "mean"), (2, "second_moment")):
                    integral = integrate.quad(lambda x: (contract.share * (x - contract.deductible))**order * dist.pdf(x),
                                              contract.deductible, cap_point, epsabs=1e-6)[0]
                    if np.isfinite(limit):
                        integral += limit**order * dist.sf(cap_point)
                    self.assertAlmostEqual(moments[field] / integral, 1, places=7)

    def test_nb_fleet_scaling(self):
        model = FrequencyModel("Negative binomial", .1, .8)
        self.assertEqual(model.fleet_moments(1000), (100., 112.5))
        mean, variance = model.fleet_moments(1000, .6)
        self.assertAlmostEqual(mean, 60.)
        self.assertAlmostEqual(variance, 64.5)
        self.assertEqual(FrequencyModel("Poisson", .1).fleet_moments(1000), (100., 100.))

    def test_tvar_uses_exact_tail_mass_and_paired_covariance(self):
        values = np.arange(1000., dtype=float)
        metrics, influence = risk_metrics(values)
        self.assertEqual(metrics["tvar99"], np.mean(values[-10:]))
        ratio = paired_tvar_ratio(metrics, metrics, influence, influence)
        self.assertEqual(ratio["tvar99_ratio_a_b"], 1.)
        self.assertEqual(ratio["ratio_mcse"], 0.)
        tied = np.concatenate((np.zeros(997), [1., 2., 3.]))
        self.assertAlmostEqual(risk_metrics(tied)[0]["tvar99"], .6)

    def test_common_claims_empty_years_thinning_and_reproducibility(self):
        config = SimulationSettings(years=1000, vehicles=1, batch_years=250, severity_grid=(1., 1.15))
        model = FrequencyModel("Negative binomial", .1, .8)
        severity = SeverityModel("Gamma", 1.4, 1200)
        contracts = [Contract("A", 100, .7, 500), Contract("B", 100, .7, 500)]
        first = _simulate_frequency_slice(model, severity, contracts, 1., config)
        again = _simulate_frequency_slice(model, severity, contracts, 1., config)
        thin = _simulate_frequency_slice(model, severity, contracts, .6, config)
        np.testing.assert_array_equal(first[0], again[0])
        np.testing.assert_array_equal(first[0][:, :, 0], first[0][:, :, 1])
        self.assertTrue(np.all(thin[2] <= first[2]))
        self.assertTrue(np.all(thin[0] <= first[0] + 1e-8))
        self.assertTrue(np.any(first[2] == 0))
        self.assertTrue(np.all(first[0][..., first[2] == 0] == 0))
        self.assertTrue(np.all(first[0][:, 0] <= first[0][:, 1]))
        self.assertTrue(np.all(first[0][1] >= first[0][0]))
        self.assertLess(first[-1], 1e-10)

    def test_current_data_models_and_equal_cost_calibration(self):
        root = Path(__file__).resolve().parents[1]
        policy, claim, details = load_training_data(root)
        self.assertEqual(details["positive_counts"], len(claim))
        self.assertGreater(details["zero_count_periods"], 0)
        frequency, frequencies = fit_frequency(policy)
        severity, severities, _ = fit_severity(claim["ClaimCharge"].to_numpy(dtype=float))
        self.assertEqual(frequencies["selected"].sum(), 1)
        self.assertEqual(severities["selected"].sum(), 1)
        contracts, calibration = calibrate_contracts(policy, claim["ClaimCharge"].to_numpy(dtype=float),
                                                      severity, read_specification(root / "spec.qmd"), SimulationSettings())
        self.assertLess(abs(calibration["relative_gap"]), 1e-8)
        self.assertEqual(contracts[0].deductible, 1000)
        self.assertEqual(contracts[1].deductible, 250)
        self.assertTrue(0 < contracts[1].share < 1)
        self.assertGreater(frequency.rate, 0)

    def test_invalid_cleaned_counts_and_exposures_are_rejected(self):
        root = Path(__file__).resolve().parents[1]
        policy, claim, _ = load_training_data(root)
        for field, value in (("ClaimNbClean", -1), ("ClaimNbClean", 123), ("Exposure", -1)):
            invalid = policy.copy()
            invalid.loc[0, field] = value
            with patch("track_4.simulation.pd.read_parquet", side_effect=[invalid, claim]):
                with self.assertRaises(ValueError):
                    load_training_data(root)


if __name__ == "__main__":
    unittest.main()
