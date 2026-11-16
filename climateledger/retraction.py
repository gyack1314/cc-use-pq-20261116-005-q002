from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from .intake import canonical_bytes
from .normalize import interval_periods, normalize_period, period_index
from .registry import ContractError, Registry, identifier


MAX_SPAN = 1000


@dataclass(frozen=True)
class RetractionNotice:
    source: str
    notice_id: str
    indicator: str
    first_period: str
    last_period: str
    reason: str
    detail: str

    @classmethod
    def from_dict(cls, value: Any, registry: Registry) -> "RetractionNotice":
        if not isinstance(value, dict):
            raise ContractError("retraction_shape", "retraction notice must be an object")
        source = identifier(value.get("source"), "source")
        notice_id = identifier(value.get("notice_id"), "notice_id")
        contract = registry.source(source)
        indicator = registry.indicator(value.get("indicator"))
        if indicator.key not in contract.allowed_indicators:
            raise ContractError("scope_violation", f"source {source} cannot retract {indicator.key}")
        first = normalize_period(value.get("first_period"), indicator.cadence)
        last = normalize_period(value.get("last_period"), indicator.cadence)
        span = period_index(last, indicator.cadence) - period_index(first, indicator.cadence) + 1
        if span < 1:
            raise ContractError("period_order", "first_period must not be after last_period")
        if span > MAX_SPAN:
            raise ContractError("retraction_span", f"retraction covers more than {MAX_SPAN} periods")
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 120:
            raise ContractError("retraction_reason", "reason must contain 1-120 characters")
        detail = value.get("detail", "")
        if not isinstance(detail, str) or len(detail) > 400:
            raise ContractError("retraction_detail", "detail must be text up to 400 characters")
        return cls(source, notice_id, indicator.key, first, last, reason.strip(), detail.strip())

    def document(self) -> dict[str, Any]:
        return asdict(self)

    def notice_hash(self) -> str:
        return sha256(canonical_bytes(self.document())).hexdigest()


class RetractionReporter:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def _progress(self, notice: dict[str, Any]) -> dict[str, Any]:
        indicator = self.registry.indicator(notice["indicator"])
        interval = interval_periods(notice["first_period"], notice["last_period"], indicator.cadence)
        if notice["status"] == "fulfilled":
            outstanding: list[str] = []
        elif notice["status"] == "applied":
            latest = self.store.latest_release()
            present = set()
            if latest:
                present = {row["period"] for row in self.store.release_entries(latest["version"])
                           if row["source"] == notice["source"] and row["indicator"] == notice["indicator"]}
            outstanding = [period for period in interval if period not in present]
        else:
            outstanding = list(interval)
        return {"interval_periods": len(interval),
                "corrected_periods": len(interval) - len(outstanding),
                "outstanding": outstanding}

    def _document(self, notice: dict[str, Any]) -> dict[str, Any]:
        return {"id": notice["id"], "source": notice["source"], "notice_id": notice["notice_id"],
                "revision": notice["revision"], "indicator": notice["indicator"],
                "first_period": notice["first_period"], "last_period": notice["last_period"],
                "reason": notice["reason"], "detail": notice["detail"],
                "notice_hash": notice["notice_hash"], "status": notice["status"],
                "received_at": notice["received_at"], "applied_version": notice["applied_version"],
                "fulfilled_version": notice["fulfilled_version"], "progress": self._progress(notice)}

    def list(self, source: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [self._document(notice) for notice in self.store.latest_retractions(status)
                if source is None or notice["source"] == source]

    def detail(self, retraction_id: int) -> dict[str, Any]:
        notice = self.store.retraction(retraction_id)
        revisions = [row for row in self.store.list_retractions()
                     if row["source"] == notice["source"] and row["notice_id"] == notice["notice_id"]]
        document = self._document(notice)
        document["events"] = self.store.events(f"retraction:{retraction_id}")
        document["revisions"] = [{"id": row["id"], "revision": row["revision"], "status": row["status"],
                                  "notice_hash": row["notice_hash"], "received_at": row["received_at"]}
                                 for row in revisions]
        return document
