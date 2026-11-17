"""State boundary: reference data, the append-only event log, batch registry,
and monthly close snapshots, with deterministic JSON persistence.

The event log is the source of truth. Closes are derived snapshots: a full
rebuild from the event log must reproduce them exactly (see pipeline.rebuild).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from .models import (
    Claim,
    FxSnapshot,
    MovementEvent,
    Risk,
    TreatyProgram,
    event_from_dict,
    event_to_dict,
    q_rate,
    rate_str,
    to_decimal,
)
from . import parser as _parser


class Store:
    def __init__(self) -> None:
        self.programs: dict[str, TreatyProgram] = {}
        self.program_docs: dict[str, dict] = {}     # canonical registration docs
        self.fx: dict[str, list[FxSnapshot]] = {}   # period -> revisions, sorted
        self.fx_docs: list[dict] = []
        self.risks: dict[str, Risk] = {}
        self.claims: dict[str, Claim] = {}
        self.events: list[MovementEvent] = []       # append-only, seq ordered
        self.movement_index: dict[str, MovementEvent] = {}   # movement_id -> first event
        self.movement_events: dict[str, list[MovementEvent]] = {}  # group by target
        self.withdrawn: set[str] = set()
        self.batches: dict[str, dict] = {}          # batch_id -> batch record
        self.rejected_attempts: list[dict] = []     # failed ingest attempts
        self.closes: dict[str, dict] = {}           # period -> close snapshot
        self.quarantine: list[dict] = []

    # ------------------------------------------------------------------ fx
    @property
    def settlement_currency(self) -> Optional[str]:
        for pid in sorted(self.programs):
            return self.programs[pid].settlement_currency
        if self.fx_docs:
            return self.fx_docs[0].get("base")
        return None

    def fx_rate(self, currency: str, period: str):
        """(snapshot_id, revision, rate) for a booking period, or None.

        Exact period match wins (highest revision); otherwise the latest
        snapshot from an earlier period is used.
        """
        settlement = self.settlement_currency
        if settlement is not None and currency == settlement:
            return ("BASE", 0, to_decimal(1))
        candidates = self.fx.get(period)
        if not candidates:
            earlier = [p for p in self.fx if p <= period]
            if not earlier:
                return None
            candidates = self.fx[max(earlier)]
        snap = candidates[-1]  # highest revision
        rate = snap.rate_for(currency)
        if rate is None:
            return None
        return (snap.snapshot_id, snap.revision, rate)

    def has_rate(self, currency: str, period: str) -> bool:
        return self.fx_rate(currency, period) is not None

    # -------------------------------------------------------------- events
    def next_seq(self) -> int:
        return len(self.events) + 1

    def add_event(self, event: MovementEvent) -> None:
        self.events.append(event)
        self.movement_index.setdefault(event.movement_id, event)
        group_key = event.target_movement_id or event.movement_id
        self.movement_events.setdefault(group_key, []).append(event)
        if event.type == "WITHDRAWAL" and event.target_movement_id:
            self.withdrawn.add(event.target_movement_id)

    def effective_amount(self, movement_id: str):
        """Current effective claim-currency amount of an ADDITION movement."""
        total = to_decimal(0)
        for e in self.movement_events.get(movement_id, []):
            if e.movement_id == movement_id and e.type == "ADDITION":
                total += e.amount
            elif e.type == "CORRECTION":
                total += e.delta_amount
        return total

    def effective_contribution(self, movement_id: str):
        """Frozen settlement-currency contribution of a movement group."""
        total = to_decimal(0)
        for e in self.movement_events.get(movement_id, []):
            total += e.contribution
        return total

    # ---------------------------------------------------------- persistence
    def to_doc(self) -> dict:
        from .report import json_ready  # local import to avoid a cycle

        return {
            "program_docs": [self.program_docs[pid] for pid in sorted(self.program_docs)],
            "fx_docs": list(self.fx_docs),
            "risks": [
                {"risk_id": r.risk_id, "treaty_program": r.treaty_program,
                 "currency": r.currency, "policy_id": r.policy_id, "cedent": r.cedent}
                for r in (self.risks[k] for k in sorted(self.risks))
            ],
            "claims": [
                {"claim_id": c.claim_id, "risk_id": c.risk_id}
                for c in (self.claims[k] for k in sorted(self.claims))
            ],
            "events": [event_to_dict(e) for e in self.events],
            "batches": [self.batches[k] for k in sorted(self.batches)],
            "rejected_attempts": list(self.rejected_attempts),
            "closes": {p: json_ready(self.closes[p]) for p in sorted(self.closes)},
            "quarantine": list(self.quarantine),
        }

    @classmethod
    def from_doc(cls, doc: dict) -> "Store":
        store = cls()
        for pd in doc.get("program_docs", []):
            prog = _parser.program_from_doc(pd)
            store.programs[prog.program_id] = prog
            store.program_docs[prog.program_id] = pd
        for fd in doc.get("fx_docs", []):
            snap = _parser.fx_from_doc(fd)
            store.fx.setdefault(snap.period, []).append(snap)
            store.fx_docs.append(fd)
        for period in store.fx:
            store.fx[period].sort(key=lambda s: s.revision)
        for rd in doc.get("risks", []):
            store.risks[rd["risk_id"]] = Risk(
                risk_id=rd["risk_id"], treaty_program=rd["treaty_program"],
                currency=rd["currency"], policy_id=rd.get("policy_id", ""),
                cedent=rd.get("cedent", ""))
        for cd in doc.get("claims", []):
            store.claims[cd["claim_id"]] = Claim(
                claim_id=cd["claim_id"], risk_id=cd["risk_id"])
        for ed in doc.get("events", []):
            store.add_event(event_from_dict(ed))
        store.batches = {b["batch_id"]: b for b in doc.get("batches", [])}
        store.rejected_attempts = list(doc.get("rejected_attempts", []))
        store.closes = dict(doc.get("closes", {}))
        store.quarantine = list(doc.get("quarantine", []))
        return store

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "store.json")
        payload = json.dumps(self.to_doc(), ensure_ascii=False, indent=2,
                             sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".store-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return path

    @classmethod
    def load(cls, directory: str) -> "Store":
        path = os.path.join(directory, "store.json")
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_doc(json.load(fh))
