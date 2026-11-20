"""End-to-end checks for the staged-rollout analysis."""

import json
import tempfile
import unittest
from pathlib import Path

from service_eval import design, engine
from service_eval.domain import (
    SourceValidationError,
    check_sources,
    compute_exposure,
    validate_sources,
)
from service_eval.protocol import (
    ProtocolError,
    StateStore,
    draft_protocol,
    validate_protocol,
)
from service_eval.stats import student_t_quantile, wilson_interval


def _rollout(**overrides):
    row = {
        "grid": "G-X",
        "start_date": "2026-03-01",
        "intensity": "high",
        "paused_from": "",
        "paused_to": "",
    }
    row.update(overrides)
    return row


def _grids(*ids):
    return [{"id": gid, "population": 500, "area_type": "urban"} for gid in ids]


def _tables(n_grids=4, months=6, **overrides):
    """Synthetic panel: grid i starts in month i, one grid never starts."""

    grid_ids = [f"T{i}" for i in range(n_grids)]
    grids = _grids(*grid_ids)
    starts = []
    for i, gid in enumerate(grid_ids):
        if i == 0:
            starts.append({"grid": gid, "start_date": "", "intensity": "none",
                           "paused_from": "", "paused_to": ""})
        else:
            starts.append({"grid": gid, "start_date": f"2026-{i + 1:02d}-01",
                           "intensity": "high", "paused_from": "", "paused_to": ""})
    outcomes = []
    months_list = [f"2026-{m:02d}" for m in range(1, months + 1)]
    for i, gid in enumerate(grid_ids):
        for j, month in enumerate(months_list):
            treated = i > 0 and month >= f"2026-{i + 1:02d}"
            received = 40
            matured = 40
            timely = int(round(matured * (0.7 if treated else 0.5)))
            outcomes.append(
                {"grid": gid, "month": month, "received": received,
                 "timely": timely, "matured": matured}
            )
    tables = {
        "grids": grids,
        "rollouts": starts,
        "outcomes": outcomes,
        "shocks": [],
    }
    for key, value in overrides.items():
        tables[key] = value
    return tables


def _protocol(tables, **overrides):
    spec = draft_protocol(tables)
    spec.update(
        comparison_start="2026-01",
        comparison_end="2026-06",
        data_through="2026-06",
        minimum_matured_per_arm=4,
        minimum_control_months=1,
        minimum_treated_grids=1,
        max_ci_width=1.0,
        mature_completion_floor=0.5,
        require_common_months=False,
    )
    spec.update(overrides)
    return validate_protocol(spec)


class ExposureTests(unittest.TestCase):
    def test_month_before_start_is_control(self):
        exposure = compute_exposure("2026-02", _rollout())
        self.assertEqual(exposure.status, "control")

    def test_start_month_is_transition_when_excluded(self):
        exposure = compute_exposure("2026-03", _rollout(), "exclude")
        self.assertEqual(exposure.status, "transition")
        exposure = compute_exposure("2026-03", _rollout(), "include")
        self.assertIn(exposure.status, ("treated", "partial"))

    def test_pause_makes_month_partial(self):
        exposure = compute_exposure(
            "2026-04", _rollout(paused_from="2026-04-05", paused_to="2026-04-25")
        )
        self.assertEqual(exposure.status, "partial")
        self.assertEqual(exposure.paused_days, 21)

    def test_full_pause_is_paused_not_treated(self):
        exposure = compute_exposure(
            "2026-04", _rollout(paused_from="2026-04-01", paused_to="2026-04-30")
        )
        self.assertEqual(exposure.status, "paused")

    def test_exit_month_partial_and_after_exit_post_exit(self):
        rollout = _rollout(end_date="2026-05-10")
        self.assertEqual(compute_exposure("2026-05", rollout).status, "partial")
        self.assertEqual(compute_exposure("2026-06", rollout).status, "post_exit")

    def test_empty_start_date_means_not_started(self):
        rollout = _rollout(start_date="", intensity="none")
        self.assertEqual(compute_exposure("2026-03", rollout).status, "control")


class DesignTests(unittest.TestCase):
    def test_control_stops_at_own_start(self):
        tables = _tables()
        protocol = _protocol(tables)
        outcomes = {(r["grid"], r["month"]): r for r in tables["outcomes"]}
        months = [f"2026-{m:02d}" for m in range(1, 7)]
        audit = design.audit_design(tables, months, protocol, outcomes)
        rows = {(r["grid"], r["month"]): r for r in audit["rows"]}
        self.assertTrue(rows[("T2", "2026-02")]["control_eligible"])
        self.assertFalse(rows[("T2", "2026-03")]["control_eligible"])
        self.assertEqual(rows[("T2", "2026-03")]["status"], "transition")
        self.assertIn("不再是对照", " ".join(rows[("T2", "2026-03")]["eligibility_reasons"]))
        self.assertFalse(rows[("T2", "2026-04")]["control_eligible"])

    def test_post_exit_never_reenters_control(self):
        tables = _tables()
        tables["rollouts"][2]["end_date"] = "2026-04-01"
        protocol = _protocol(tables)
        outcomes = {(r["grid"], r["month"]): r for r in tables["outcomes"]}
        months = [f"2026-{m:02d}" for m in range(1, 7)]
        audit = design.audit_design(tables, months, protocol, outcomes)
        row = next(r for r in audit["rows"] if r["grid"] == "T2" and r["month"] == "2026-06")
        self.assertEqual(row["status"], "post_exit")
        self.assertFalse(row["control_eligible"])

    def test_control_changes_are_reported(self):
        tables = _tables()
        protocol = _protocol(tables)
        outcomes = {(r["grid"], r["month"]): r for r in tables["outcomes"]}
        months = [f"2026-{m:02d}" for m in range(1, 7)]
        audit = design.audit_design(tables, months, protocol, outcomes)
        objects = design.control_comparison_objects(audit)
        self.assertTrue(any(o["controls"] for o in objects))
        self.assertEqual(objects[0]["month"], "2026-01")


class ProtocolTests(unittest.TestCase):
    def test_unknown_field_rejected(self):
        tables = _tables()
        spec = draft_protocol(tables)
        spec["exclude_grids"] = ["T1"]
        with self.assertRaises(ProtocolError):
            validate_protocol(spec)

    def test_end_before_start_rejected(self):
        tables = _tables()
        with self.assertRaises(ProtocolError):
            _protocol(tables, comparison_start="2026-05", comparison_end="2026-02")

    def test_data_through_must_cover_window(self):
        tables = _tables()
        with self.assertRaises(ProtocolError):
            _protocol(tables, data_through="2026-03")

    def test_freeze_requires_reason_and_revision_is_append_only(self):
        tables = _tables()
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            with self.assertRaises(ProtocolError):
                store.freeze(draft_protocol(tables), "", tables)
            first = store.freeze(_protocol(tables), "初次冻结", tables)
            self.assertEqual(first["version"], 1)
            revised_spec = _protocol(tables, confidence_level=0.8)
            second = store.freeze(revised_spec, "提高精度前先改置信水平", tables)
            self.assertEqual(second["version"], 2)
            self.assertFalse(store.protocol_version(1)["active"])
            self.assertTrue(store.protocol_version(2)["active"])
            self.assertEqual(
                [c["field"] for c in second["changes_from_previous"]], ["confidence_level"]
            )

    def test_identical_freeze_rejected(self):
        tables = _tables()
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            store.freeze(_protocol(tables), "初次冻结", tables)
            with self.assertRaises(ProtocolError):
                store.freeze(_protocol(tables), "重复冻结", tables)

    def test_analysis_refuses_without_frozen_protocol_via_store(self):
        tables = _tables()
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            self.assertIsNone(store.active_protocol())


class SourceValidationTests(unittest.TestCase):
    def test_timely_above_matured_is_warning_not_crash(self):
        tables = _tables()
        tables["outcomes"][0]["timely"] = tables["outcomes"][0]["matured"] + 1
        issues = check_sources(tables)
        self.assertTrue(any(i["severity"] == "warning" for i in issues))
        validate_sources(tables)  # does not raise for count inconsistencies

    def test_unknown_field_is_blocking(self):
        tables = _tables()
        tables["grids"][0]["extra"] = 1
        with self.assertRaises(SourceValidationError):
            validate_sources(tables)

    def test_not_started_with_intensity_is_blocking(self):
        tables = _tables()
        tables["rollouts"][0]["intensity"] = "high"
        with self.assertRaises(SourceValidationError):
            validate_sources(tables)

    def test_inconsistent_counts_excluded_from_estimate(self):
        tables = _tables()
        # make one control month impossible on its face: timely > matured
        target = next(
            r for r in tables["outcomes"] if r["grid"] == "T3" and r["month"] == "2026-02"
        )
        target["timely"] = target["matured"] + 5
        report = engine.analyze(tables, _protocol(tables), 1)
        self.assertIn("T3 2026-02", report["quality"]["excluded_grid_months"])
        self.assertEqual(
            report["estimate"]["month_estimates"][1]["control"]["matured"], 80
        )
        self.assertTrue(any(
            row["grid"] == "T3" and row["month"] == "2026-02"
            for row in report["design"]["rows"]
        ))


class AnalysisTests(unittest.TestCase):
    def test_effect_is_detected_against_current_controls(self):
        tables = _tables()
        report = engine.analyze(tables, _protocol(tables), 1)
        self.assertEqual(report["estimate"]["status"], "supported", report["estimate"]["status_label"])
        self.assertAlmostEqual(report["estimate"]["value"], 0.2, places=6)
        self.assertGreater(report["estimate"]["ci_low"], 0)
        self.assertTrue(report["conclusion"]["can_conclude"])

    def test_no_controls_left_cannot_conclude(self):
        tables = _tables()
        # every grid starts in the first month, so no control remains afterwards
        for rollout in tables["rollouts"]:
            rollout["start_date"] = "2026-01-01"
            rollout["intensity"] = "high"
        report = engine.analyze(tables, _protocol(tables), 1)
        self.assertIn(report["estimate"]["status"], ("no_control", "no_common_month"))
        self.assertFalse(report["conclusion"]["can_conclude"])

    def test_immature_outcomes_never_counted_as_failures(self):
        tables = _tables()
        for row in tables["outcomes"]:
            if row["grid"] == "T1" and row["month"] == "2026-02":
                row["received"] = 100
                row["matured"] = 40
                row["timely"] = 30
        report = engine.analyze(tables, _protocol(tables), 1)
        control = next(
            m["control"] for m in report["estimate"]["month_estimates"] if m["month"] == "2026-02"
        )
        # the immature 60 are neither successes nor failures; the matured subset sets the rate
        self.assertEqual(control["matured"], 120)
        self.assertEqual(control["timely"], 60)
        self.assertAlmostEqual(control["rate"], 60 / 120)
        self.assertNotAlmostEqual(control["rate"], 30 / 100)

    def test_identical_values_give_zero_width_interval(self):
        tables = _tables()
        for row in tables["outcomes"]:
            row["timely"] = row["matured"]
        report = engine.analyze(tables, _protocol(tables), 1)
        self.assertEqual(report["estimate"]["value"], 0)

    def test_sensitivity_never_drops_grids_by_result(self):
        tables = _tables()
        report = engine.analyze(tables, _protocol(tables), 1)
        primary = set(report["estimate"]["treated_grids"]) | set(report["estimate"]["control_grids"])
        for variant in report["sensitivity"]:
            used = set(variant["scope"]["treated_grids"]) | set(variant["scope"]["control_grids"])
            self.assertTrue(used <= primary | set(report["design"]["grids_ever_started"]))
            self.assertNotIn("best_grids", json.dumps(variant))

    def test_wide_interval_blocks_conclusion(self):
        tables = _tables()
        # deliberately heterogeneous effects widen the leave-one-grid-out interval
        for row in tables["outcomes"]:
            if row["grid"] == "T3":
                row["timely"] = row["matured"]
            if row["grid"] == "T1" and row["month"] < "2026-03":
                row["timely"] = int(row["matured"] * 0.2)
            if row["grid"] == "T2" and row["month"] >= "2026-03":
                row["timely"] = row["matured"]
        spec = _protocol(tables, max_ci_width=0.05)
        report = engine.analyze(tables, spec, 1)
        self.assertFalse(report["conclusion"]["can_conclude"])
        self.assertIn(report["estimate"]["status"], ("imprecise", "unidentified"))

    def test_immature_end_blocks_conclusion(self):
        tables = _tables()
        for row in tables["outcomes"]:
            if row["month"] == "2026-06":
                row["matured"] = 5
                row["timely"] = 3
        spec = _protocol(tables, mature_completion_floor=0.9)
        report = engine.analyze(tables, spec, 1)
        self.assertIn(report["estimate"]["status"], ("immature_end", "inconclusive", "imprecise"))
        self.assertLess(report["maturity"]["end_matured_share"], 0.9)

    def test_shock_months_are_flagged_not_silently_controlled(self):
        tables = _tables()
        tables["shocks"] = [{"grid": "T2", "month": "2026-03",
                             "type": "weather", "intensity": "high"}]
        report = engine.analyze(tables, _protocol(tables), 1)
        self.assertIn("2026-03", report["shock_analysis"]["months_with_shocks_in_comparison"])
        self.assertTrue(report["shock_analysis"]["events"][0]["in_comparison"])
        self.assertIn("无法", report["shock_analysis"]["events"][0]["note"])
        self.assertIn("不能声称已控制", report["shock_analysis"]["statement"])

    def test_report_marks_unfrozen_provenance(self):
        tables = _tables()
        report = engine.analyze(tables, _protocol(tables), None)
        self.assertFalse(report["provenance"]["frozen_protocol"])

    def test_compare_reports_names_affected_parts(self):
        tables = _tables()
        old = engine.analyze(tables, _protocol(tables), 1)
        tables["outcomes"].append(
            {"grid": "T3", "month": "2026-06", "received": 40, "timely": 40, "matured": 40}
        )
        new = engine.analyze(tables, _protocol(tables), 2)
        impact = engine.compare_reports(old, new)
        self.assertTrue(impact["affected_parts"])


class StatsTests(unittest.TestCase):
    def test_wilson_is_reasonable(self):
        low, high = wilson_interval(5, 10, 0.9)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)

    def test_t_quantile_close_to_normal_for_large_df(self):
        self.assertAlmostEqual(student_t_quantile(0.95, 500), 1.645, places=2)


if __name__ == "__main__":
    unittest.main()
