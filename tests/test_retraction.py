from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from climateledger.cli import main
from climateledger.intake import write_manifest
from climateledger.registry import ContractError
from climateledger.service import LedgerService
from support import LedgerCase


class RetractionTests(LedgerCase):
    def notice(self, notice_id="calib_drift", first="2026-02", last="2026-03", **overrides):
        value = {"source": "lab_a", "notice_id": notice_id, "indicator": "co2_ppm",
                 "first_period": first, "last_period": last,
                 "reason": "wrong_calibration", "detail": "Used calibration record K-41"}
        value.update(overrides)
        return value

    def record(self, **overrides):
        return self.service.record_retraction(self.notice(**overrides))

    def test_record_is_idempotent(self):
        first = self.record()
        self.assertFalse(first["idempotent"])
        self.assertEqual(first["retraction"]["status"], "open")
        again = self.record()
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["retraction"]["id"], first["retraction"]["id"])
        self.assertEqual(len(self.service.store.list_retractions()), 1)

    def test_changed_notice_creates_new_revision(self):
        first = self.record()
        changed = self.record(last="2026-04")
        self.assertEqual(changed["retraction"]["revision"], 2)
        self.assertNotEqual(changed["retraction"]["id"], first["retraction"]["id"])
        listing = self.service.retractions()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["revision"], 2)
        self.assertEqual(listing[0]["last_period"], "2026-04")

    def test_same_content_after_change_is_new_revision(self):
        self.record()
        self.record(last="2026-04")
        restored = self.record()
        self.assertEqual(restored["retraction"]["revision"], 3)
        self.assertEqual(len(self.service.store.list_retractions()), 3)

    def test_record_validation(self):
        with self.assertRaisesRegex(ContractError, "unknown_indicator"):
            self.record(indicator="missing")
        with self.assertRaisesRegex(ContractError, "unknown_source"):
            self.record(source="lab_z")
        with self.assertRaisesRegex(ContractError, "period_format"):
            self.record(first="2026-1")
        with self.assertRaisesRegex(ContractError, "period_order"):
            self.record(first="2026-04", last="2026-02")
        with self.assertRaisesRegex(ContractError, "retraction_span"):
            self.record(first="1900-01", last="2100-01")
        with self.assertRaisesRegex(ContractError, "scope_violation"):
            self.record(source="lab_b", indicator="fire_area")
        with self.assertRaisesRegex(ContractError, "retraction_reason"):
            self.record(reason="")
        with self.assertRaisesRegex(ContractError, "invalid_identifier"):
            self.record(notice_id="Bad Id")
        self.assertEqual(self.service.store.list_retractions(), [])

    def test_apply_removes_only_target_interval(self):
        self.release("base", self.rows(428, 429, 430, 431))
        other, _ = self.ingest("other", self.rows(427, 426), source="lab_b")
        self.service.publish(other["id"], 1)
        notice = self.record()["retraction"]
        applied = self.service.apply_retraction(notice["id"], 1)
        self.assertEqual(applied["version"], 3)
        self.assertIsNone(applied["manifest"]["batch"])
        self.assertEqual(applied["manifest"]["retractions"][0]["removed_entries"], 2)
        self.assertEqual(applied["manifest"]["retractions"][0]["notice_id"], "calib_drift")
        entries = self.service.export(3)["entries"]
        lab_a = sorted(row["period"] for row in entries if row["source"] == "lab_a")
        lab_b = sorted(row["period"] for row in entries if row["source"] == "lab_b")
        self.assertEqual(lab_a, ["2026-01", "2026-04"])
        self.assertEqual(lab_b, ["2026-01", "2026-02"])
        self.assertTrue(self.service.verify(self.service.export(3)))

    def test_apply_is_idempotent_across_reopen(self):
        self.release("base", self.rows(428, 429, 430))
        notice = self.record()["retraction"]
        applied = self.service.apply_retraction(notice["id"], 1)
        again = self.service.apply_retraction(notice["id"], 1)
        self.assertEqual(again["digest"], applied["digest"])
        self.assertEqual(self.service.status()["latest_release"], 2)
        with LedgerService(self.db) as reopened:
            third = reopened.apply_retraction(notice["id"], 1)
            self.assertEqual(third["digest"], applied["digest"])
            self.assertEqual(reopened.status()["latest_release"], 2)

    def test_apply_rejects_unknown_and_superseded(self):
        with self.assertRaisesRegex(ContractError, "unknown_retraction"):
            self.service.apply_retraction(99, 1)
        notice = self.record()["retraction"]
        self.record(last="2026-04")
        with self.assertRaisesRegex(ContractError, "stale_revision"):
            self.service.apply_retraction(notice["id"], 1)

    def test_old_release_remains_verifiable(self):
        self.release("base", self.rows(428, 429, 430))
        notice = self.record()["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        old = self.service.export(1)
        self.assertTrue(self.service.verify(old))
        self.assertEqual([row["period"] for row in old["entries"]],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertEqual([row["period"] for row in self.service.series("co2_ppm", 1)],
                         ["2026-01", "2026-02", "2026-03"])
        self.assertEqual([row["period"] for row in self.service.series("co2_ppm")], ["2026-01"])

    def test_correction_fulfills_after_apply(self):
        self.release("base", self.rows(428, 429, 430))
        notice = self.record()["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        correction, _ = self.ingest("correction", self.rows("429.10", "430.20", start=2))
        self.assertEqual(correction["status"], "accepted")
        published = self.service.publish(correction["id"], 1)
        self.assertEqual(published["version"], 3)
        state = self.service.retraction(notice["id"])
        self.assertEqual(state["status"], "fulfilled")
        self.assertEqual(state["applied_version"], 2)
        self.assertEqual(state["fulfilled_version"], 3)
        self.assertEqual(state["progress"]["outstanding"], [])
        values = {row["period"]: row["value"] for row in self.service.series("co2_ppm")}
        self.assertEqual(values["2026-02"], "429.10")
        self.assertEqual(values["2026-03"], "430.20")

    def test_partial_correction_shows_pending_and_progress(self):
        self.release("base", self.rows(428, 429, 430, 431))
        notice = self.record(last="2026-04")["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        partial, _ = self.ingest("partial", self.rows("429.10", start=2))
        self.assertEqual(partial["status"], "review")
        pending = sorted(item["period"] for item in partial["findings"]
                         if item["code"] == "retraction_pending")
        self.assertEqual(pending, ["2026-03", "2026-04"])
        self.service.approve(partial["id"], 1)
        self.service.publish(partial["id"], 1)
        state = self.service.retractions()[0]
        self.assertEqual(state["status"], "applied")
        self.assertEqual(state["progress"]["outstanding"], ["2026-03", "2026-04"])
        self.assertEqual(state["progress"]["corrected_periods"], 1)
        self.assertEqual([row["period"] for row in self.service.series("co2_ppm")],
                         ["2026-01", "2026-02"])
        coverage = self.service.coverage(3)
        self.assertEqual(coverage["retractions"][0]["outstanding"], ["2026-03", "2026-04"])
        rest, _ = self.ingest("rest", self.rows("430.20", "431.30", start=3))
        self.assertEqual(rest["status"], "accepted")
        self.service.publish(rest["id"], 1)
        self.assertEqual(self.service.retraction(notice["id"])["status"], "fulfilled")
        self.assertEqual(self.service.coverage(4)["retractions"], [])

    def test_single_release_presents_retraction_and_correction(self):
        self.release("base", self.rows(428, 429, 430))
        notice = self.record()["retraction"]
        correction, _ = self.ingest("correction", self.rows("429.10", "430.20", start=2))
        self.assertEqual(correction["status"], "review")
        codes = {item["code"] for item in correction["findings"]}
        self.assertIn("revised_value", codes)
        self.assertNotIn("retraction_pending", codes)
        self.service.approve(correction["id"], 1)
        published = self.service.publish(correction["id"], 1)
        self.assertIsNotNone(published["manifest"]["batch"])
        self.assertEqual(len(published["manifest"]["retractions"]), 1)
        state = self.service.retraction(notice["id"])
        self.assertEqual(state["status"], "fulfilled")
        self.assertEqual(state["applied_version"], 2)
        self.assertEqual(state["fulfilled_version"], 2)
        diff = self.service.compare(1, 2)
        self.assertEqual(diff["summary"]["by_kind"]["revised"], 2)
        self.assertEqual(diff["summary"]["by_kind"]["removed"], 0)

    def test_unrelated_publish_sweeps_open_retraction(self):
        self.release("base", self.rows(428, 429))
        self.record(first="2026-02", last="2026-02")
        other, _ = self.ingest("other", self.rows(427), source="lab_b")
        self.assertEqual(other["status"], "accepted")
        published = self.service.publish(other["id"], 1)
        self.assertEqual(len(published["manifest"]["retractions"]), 1)
        periods = {(row["source"], row["period"]) for row in self.service.export(2)["entries"]}
        self.assertNotIn(("lab_a", "2026-02"), periods)
        self.assertIn(("lab_a", "2026-01"), periods)
        self.assertIn(("lab_b", "2026-01"), periods)

    def test_retraction_without_published_entries(self):
        notice = self.record(first="2026-10", last="2026-11")["retraction"]
        applied = self.service.apply_retraction(notice["id"], 1)
        self.assertEqual(applied["version"], 1)
        self.assertEqual(applied["manifest"]["retractions"][0]["removed_entries"], 0)
        self.assertEqual(applied["manifest"]["entry_count"], 0)

    def test_revised_notice_reapplies(self):
        self.release("base", self.rows(428, 429, 430, 431))
        notice = self.record(first="2026-02", last="2026-03")["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        widened = self.record(first="2026-02", last="2026-04")["retraction"]
        self.assertEqual(widened["revision"], 2)
        applied = self.service.apply_retraction(widened["id"], 2)
        self.assertEqual(applied["manifest"]["retractions"][0]["removed_entries"], 1)
        self.assertEqual([row["period"] for row in self.service.series("co2_ppm")], ["2026-01"])

    def test_batch_outside_retraction_has_no_pending_findings(self):
        self.release("base", self.rows(428, 429, 430))
        self.record()
        batch, _ = self.ingest("later", self.rows(431, start=4))
        self.assertEqual(batch["status"], "accepted")
        self.assertNotIn("retraction_pending", {item["code"] for item in batch["findings"]})

    def test_events_and_audit_trail(self):
        self.release("base", self.rows(428, 429))
        notice = self.record(first="2026-02", last="2026-02")["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        correction, _ = self.ingest("correction", self.rows("429.10", start=2))
        self.service.publish(correction["id"], 1)
        events = self.service.events(f"retraction:{notice['id']}")
        self.assertEqual([event["kind"] for event in events],
                         ["retract", "apply_retraction", "fulfill_retraction"])
        report = self.service.audit_all()
        self.assertEqual(report["violations"], [])
        self.assertEqual(report["retraction_violations"], [])
        self.assertEqual(report["retractions"][0]["status"], "fulfilled")

    def test_detail_lists_revisions_and_events(self):
        notice = self.record()["retraction"]
        self.record(last="2026-04")
        detail = self.service.retraction(notice["id"])
        self.assertEqual([row["revision"] for row in detail["revisions"]], [1, 2])
        self.assertEqual([event["kind"] for event in detail["events"]], ["retract"])

    def test_reporter_filters_and_status_counts(self):
        self.record()
        self.record(notice_id="other_notice", source="lab_b")
        self.assertEqual(len(self.service.retractions()), 2)
        self.assertEqual(len(self.service.retractions(source="lab_a")), 1)
        self.assertEqual(len(self.service.retractions(status="open")), 2)
        self.assertEqual(len(self.service.retractions(status="fulfilled")), 0)
        self.assertEqual(self.service.status()["retractions"],
                         {"open": 2, "applied": 0, "fulfilled": 0})

    def test_trailing_retraction_visible_in_coverage(self):
        self.release("base", self.rows(428, 429, 430))
        notice = self.record(first="2026-03", last="2026-03")["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        coverage = self.service.coverage(2)
        self.assertEqual(coverage["missing"], 0)
        self.assertEqual(coverage["retractions"][0]["outstanding"], ["2026-03"])

    def test_record_after_fulfillment_stays_idempotent(self):
        self.release("base", self.rows(428, 429))
        notice = self.record(first="2026-02", last="2026-02")["retraction"]
        self.service.apply_retraction(notice["id"], 1)
        correction, _ = self.ingest("correction", self.rows("429.10", start=2))
        self.service.publish(correction["id"], 1)
        again = self.record(first="2026-02", last="2026-02")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.service.store.list_retractions()), 1)


class RetractionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "cli.sqlite3"

    def call(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def contract(self, filename, data):
        path = self.root / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_cli_retraction_workflow(self):
        indicator = self.contract("indicator.json", {"key": "co2_ppm", "title": "Synthetic carbon dioxide",
            "canonical_unit": "ppm", "cadence": "month", "precision": 2})
        source = self.contract("source.json", {"key": "lab_a", "title": "Synthetic lab A",
            "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        self.assertEqual(self.call("register-indicator", indicator)[0], 0)
        self.assertEqual(self.call("register-source", source)[0], 0)
        manifest = write_manifest(self.root, source="lab_a", batch_id="base",
                                  rows=[{"indicator": "co2_ppm", "period": "2026-01",
                                         "value": "428", "unit": "ppm"},
                                        {"indicator": "co2_ppm", "period": "2026-02",
                                         "value": "429", "unit": "ppm"}])
        code, ingestion, _ = self.call("ingest", str(manifest))
        self.assertEqual(code, 0)
        self.assertEqual(self.call("publish", str(ingestion["batch"]["id"]), "1")[0], 0)
        notice_path = self.contract("notice.json", {"source": "lab_a", "notice_id": "calib_drift",
            "indicator": "co2_ppm", "first_period": "2026-02", "last_period": "2026-02",
            "reason": "wrong_calibration", "detail": "demo"})
        code, recorded, _ = self.call("retract", notice_path)
        self.assertEqual(code, 0)
        self.assertFalse(recorded["idempotent"])
        code, repeated, _ = self.call("retract", notice_path)
        self.assertTrue(repeated["idempotent"])
        code, applied, _ = self.call("apply-retraction", "1", "1")
        self.assertEqual(code, 0)
        self.assertEqual(applied["version"], 2)
        self.assertIsNone(applied["manifest"]["batch"])
        code, listing, _ = self.call("retractions", "--status", "applied")
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["progress"]["outstanding"], ["2026-02"])
        code, detail, _ = self.call("retraction", "1")
        self.assertEqual(detail["status"], "applied")
        self.assertEqual([event["kind"] for event in detail["events"]],
                         ["retract", "apply_retraction"])
        bundle_path = self.root / "bundle.json"
        self.assertEqual(self.call("export", "1", "--output", str(bundle_path))[0], 0)
        self.assertTrue(self.call("verify", str(bundle_path))[1]["verified"])
        bad = self.contract("bad_notice.json", {"source": "lab_a", "notice_id": "bad_notice",
            "indicator": "co2_ppm", "first_period": "2026-03", "last_period": "2026-02",
            "reason": "wrong_calibration"})
        code, _, error = self.call("retract", bad)
        self.assertEqual(code, 2)
        self.assertIn("period_order", error)
