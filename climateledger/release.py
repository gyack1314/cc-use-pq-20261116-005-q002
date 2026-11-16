from __future__ import annotations

from hashlib import sha256
from typing import Any

from .intake import canonical_bytes
from .normalize import interval_periods
from .registry import ContractError


class ReleaseBuilder:
    def __init__(self, store, registry):
        self.store = store
        self.registry = registry

    def preview(self, batch_id: int, expected_revision: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        if batch["revision"] != expected_revision:
            raise ContractError("stale_revision", "expected revision differs")
        if batch["status"] not in {"accepted", "approved", "published"}:
            raise ContractError("quality_gate", "batch has unresolved quality findings")
        latest = self.store.latest_batch(batch["source"], batch["external_id"])
        if latest["id"] != batch_id and batch["status"] != "published":
            raise ContractError("stale_revision", "newer revision supersedes this batch")
        if batch["published_version"]:
            return self.store.release(batch["published_version"])["manifest"]
        prior = self.store.latest_release()
        version = prior["version"] + 1 if prior else 1
        entries, applied = self._plan(batch_id, prior["version"] if prior else None)
        return self._manifest(batch, version, entries, prior, applied)

    def _plan(self, batch_id: int | None, prior_version: int | None):
        entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        if prior_version:
            for item in self.store.release_entries(prior_version):
                key = item["source"], item["indicator"], item["period"]
                entries[key] = {field: item[field] for field in (
                    "source", "indicator", "period", "batch_id", "line", "value", "unit")}
        applied = []
        for notice in self.store.latest_retractions("open"):
            removed = 0
            for key, entry in list(entries.items()):
                if (entry["source"] == notice["source"] and entry["indicator"] == notice["indicator"]
                        and notice["first_period"] <= entry["period"] <= notice["last_period"]):
                    del entries[key]
                    removed += 1
            applied.append({"id": notice["id"], "source": notice["source"],
                            "notice_id": notice["notice_id"], "revision": notice["revision"],
                            "indicator": notice["indicator"], "first_period": notice["first_period"],
                            "last_period": notice["last_period"], "reason": notice["reason"],
                            "removed_entries": removed})
        if batch_id is not None:
            batch = self.store.batch(batch_id)
            for item in self.store.measurements(batch_id):
                key = batch["source"], item["indicator"], item["period"]
                entries[key] = {"source": batch["source"], "indicator": item["indicator"],
                                "period": item["period"], "batch_id": batch_id, "line": item["line"],
                                "value": item["value"], "unit": item["unit"]}
        return [entries[key] for key in sorted(entries)], applied

    def _fulfilled_retractions(self, entries: list[dict[str, Any]],
                               applied: list[dict[str, Any]]) -> list[int]:
        present = {(item["source"], item["indicator"], item["period"]) for item in entries}
        candidates = {notice["id"]: notice for notice in self.store.latest_retractions("applied")}
        for notice in applied:
            candidates.setdefault(notice["id"], notice)
        fulfilled = []
        for notice in candidates.values():
            indicator = self.registry.indicator(notice["indicator"])
            if all((notice["source"], notice["indicator"], period) in present
                   for period in interval_periods(notice["first_period"], notice["last_period"],
                                                  indicator.cadence)):
                fulfilled.append(notice["id"])
        return sorted(fulfilled)

    def _manifest(self, batch: dict[str, Any] | None, version: int,
                  entries: list[dict[str, Any]], prior: dict[str, Any] | None,
                  applied: list[dict[str, Any]]) -> dict[str, Any]:
        data_hash = sha256(canonical_bytes(entries)).hexdigest()
        return {"schema_version": 1, "version": version,
                "parent_digest": prior["digest"] if prior else None,
                "batch": {"id": batch["id"], "source": batch["source"], "external_id": batch["external_id"],
                          "revision": batch["revision"], "payload_hash": batch["payload_hash"],
                          "row_count": batch["row_count"]} if batch else None,
                "retractions": applied,
                "entry_count": len(entries), "entries_sha256": data_hash,
                "quality_findings": batch["findings"] if batch else []}

    def publish(self, batch_id: int, expected_revision: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        if batch["published_version"]:
            if batch["revision"] != expected_revision:
                raise ContractError("stale_revision", "expected revision differs")
            return self.store.release(batch["published_version"])
        manifest = self.preview(batch_id, expected_revision)
        prior = self.store.latest_release()
        entries, applied = self._plan(batch_id, prior["version"] if prior else None)
        fulfilled = self._fulfilled_retractions(entries, applied)
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return self.store.publish(batch_id, expected_revision, manifest, entries, digest,
                                  [item["id"] for item in applied], fulfilled)

    def apply_retraction(self, retraction_id: int, expected_revision: int) -> dict[str, Any]:
        notice = self.store.retraction(retraction_id)
        if notice["applied_version"] is not None:
            if notice["revision"] != expected_revision:
                raise ContractError("stale_revision", "expected revision differs")
            return self.store.release(notice["applied_version"])
        latest = self.store.latest_retraction(notice["source"], notice["notice_id"])
        if latest["id"] != retraction_id or notice["revision"] != expected_revision:
            raise ContractError("stale_revision", "cannot apply a superseded retraction")
        prior = self.store.latest_release()
        version = prior["version"] + 1 if prior else 1
        entries, applied = self._plan(None, prior["version"] if prior else None)
        if not any(item["id"] == retraction_id for item in applied):
            raise ContractError("release_race", "retraction state changed while preparing")
        fulfilled = self._fulfilled_retractions(entries, applied)
        manifest = self._manifest(None, version, entries, prior, applied)
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        return self.store.publish_retraction(retraction_id, expected_revision, manifest, entries, digest,
                                             [item["id"] for item in applied], fulfilled)

    def export(self, version: int) -> dict[str, Any]:
        release = self.store.release(version)
        entries = self.store.release_entries(version)
        comparable = [{key: item[key] for key in ("source", "indicator", "period", "batch_id", "line", "value", "unit")}
                      for item in entries]
        actual = sha256(canonical_bytes(comparable)).hexdigest()
        if actual != release["manifest"]["entries_sha256"]:
            raise ContractError("release_corrupt", "release entries digest differs from manifest")
        manifest_digest = sha256(canonical_bytes(release["manifest"])).hexdigest()
        if manifest_digest != release["digest"]:
            raise ContractError("release_corrupt", "manifest digest differs from stored digest")
        if release["manifest"]["entry_count"] != len(entries):
            raise ContractError("release_corrupt", "release entry count differs")
        return {"manifest": release["manifest"], "digest": release["digest"], "entries": comparable}

    @staticmethod
    def verify(bundle: dict[str, Any]) -> bool:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("manifest"), dict):
            return False
        if not isinstance(bundle.get("entries"), list) or not isinstance(bundle.get("digest"), str):
            return False
        manifest = bundle["manifest"]
        if manifest.get("entry_count") != len(bundle["entries"]):
            return False
        try:
            entries_hash = sha256(canonical_bytes(bundle["entries"])).hexdigest()
            manifest_hash = sha256(canonical_bytes(manifest)).hexdigest()
        except (TypeError, ValueError):
            return False
        return entries_hash == manifest.get("entries_sha256") and manifest_hash == bundle["digest"]

    def lineage(self, version: int) -> list[dict[str, Any]]:
        seen = set()
        result = []
        current = self.store.release(version)
        while current:
            if current["digest"] in seen:
                raise ContractError("release_cycle", "digest chain repeats")
            seen.add(current["digest"])
            result.append({"version": current["version"], "digest": current["digest"],
                           "batch": current["manifest"].get("batch"),
                           "retractions": current["manifest"].get("retractions", [])})
            if current["version"] == 1:
                break
            previous = self.store.release(current["version"] - 1)
            if current["manifest"]["parent_digest"] != previous["digest"]:
                raise ContractError("release_corrupt", "parent digest chain differs")
            current = previous
        return result
