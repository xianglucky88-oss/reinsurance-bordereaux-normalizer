"""Orchestration boundary: register reference data, ingest batches, close
periods, rebuild from the event log, and replay treaty versions.

Processing boundaries
---------------------
* event-level increment: every confirmed movement is appended to the
  immutable event log with its FX conversion frozen on the event;
* monthly snapshot: ``close_all`` derives one close per booking period and
  never mutates a closed period — late movements land in the period they are
  booked in;
* full rebuild: ``rebuild`` replays the whole event log and must reproduce
  every stored close exactly, which is the audit-consistency guarantee.
"""
from __future__ import annotations

import json
from typing import Optional

from . import engine, parser
from .models import (
    ZERO,
    BatchResult,
    Claim,
    DomainError,
    MovementEvent,
    Risk,
    RowError,
    periods_between,
    prev_period,
    q_money,
)
from .report import build_result, json_ready
from .store import Store

PROJECT = "reinsurance-bordereaux-normalizer"


class Pipeline:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()

    # ------------------------------------------------------- reference data
    def register_reference(self, doc: dict) -> dict:
        if not isinstance(doc, dict):
            return {"status": "error", "reason": "reference must be an object"}
        out = {"programs": [], "fx": []}
        for pd in doc.get("programs", []) or []:
            out["programs"].append(self.register_program(pd))
        for fd in doc.get("fx", []) or []:
            out["fx"].append(self.register_fx(fd))
        ok = all(r["status"] == "ok" for r in out["programs"] + out["fx"])
        out["status"] = "ok" if ok else "error"
        return out

    def register_program(self, doc: dict) -> dict:
        try:
            prog = parser.program_from_doc(doc)
        except DomainError as exc:
            return {"status": "error", "reason": str(exc)}

        settlement = self.store.settlement_currency
        if settlement is not None and prog.settlement_currency != settlement:
            return {"status": "error",
                    "reason": f"settlement currency {prog.settlement_currency} "
                              f"does not match existing {settlement}"}
        fx_base = self.store.fx_docs[0]["base"] if self.store.fx_docs else None
        if fx_base is not None and fx_base != prog.settlement_currency:
            return {"status": "error",
                    "reason": f"settlement currency {prog.settlement_currency} "
                              f"does not match FX base {fx_base}"}

        existing = self.store.programs.get(prog.program_id)
        if existing is not None:
            old_doc = self.store.program_docs[prog.program_id]
            old_versions = {v["version"]: v for v in old_doc["versions"]}
            new_versions = {v["version"]: v for v in doc["versions"]}
            for vid, vdoc in old_versions.items():
                if vid not in new_versions:
                    return {"status": "error",
                            "reason": f"treaty version {vid} cannot be removed"}
                changed = set(vdoc) | set(new_versions[vid])
                changed = {k for k in changed
                           if json.dumps(vdoc.get(k), sort_keys=True)
                           != json.dumps(new_versions[vid].get(k), sort_keys=True)}
                if changed - {"effective_to"}:
                    return {"status": "error",
                            "reason": f"treaty version {vid} is immutable once registered "
                                      f"(changed: {sorted(changed - {'effective_to'})})"}
            # a version change may only govern its own effective range: it must
            # not change which version applies to any already-closed period
            for period in sorted(self.store.closes):
                before = existing.version_for(period)
                after = prog.version_for(period)
                if (before is None) != (after is None) or (
                        before is not None and before.version != after.version):
                    return {"status": "error",
                            "reason": f"version change would rewrite closed period {period}"}
        self.store.programs[prog.program_id] = prog
        self.store.program_docs[prog.program_id] = json.loads(json.dumps(doc))
        return {"status": "ok", "program_id": prog.program_id,
                "versions": [v.version for v in prog.versions]}

    def register_fx(self, doc: dict) -> dict:
        try:
            snap = parser.fx_from_doc(doc)
        except DomainError as exc:
            return {"status": "error", "reason": str(exc)}
        settlement = self.store.settlement_currency
        if settlement is not None and snap.base != settlement:
            return {"status": "error",
                    "reason": f"FX base {snap.base} does not match settlement "
                              f"currency {settlement}"}
        revisions = self.store.fx.setdefault(snap.period, [])
        for existing in revisions:
            if existing.snapshot_id == snap.snapshot_id and existing.revision == snap.revision:
                if existing.rates != snap.rates or existing.base != snap.base:
                    return {"status": "error",
                            "reason": f"FX snapshot {snap.snapshot_id} revision "
                                      f"{snap.revision} is immutable once registered"}
                return {"status": "ok", "snapshot_id": snap.snapshot_id,
                        "revision": snap.revision, "duplicate": True}
        revisions.append(snap)
        revisions.sort(key=lambda s: (s.revision, s.snapshot_id))
        self.store.fx_docs.append(json.loads(json.dumps(doc)))
        return {"status": "ok", "snapshot_id": snap.snapshot_id, "revision": snap.revision}

    # -------------------------------------------------------------- ingest
    def _known_view(self) -> parser.KnownView:
        return parser.KnownView(
            programs=set(self.store.programs),
            risks=dict(self.store.risks),
            claims=dict(self.store.claims),
            movement_fingerprints={mid: e.fingerprint()
                                   for mid, e in self.store.movement_index.items()},
            movement_types={mid: e.type for mid, e in self.store.movement_index.items()},
            movement_claims={mid: e.claim_id for mid, e in self.store.movement_index.items()},
            movement_currencies={mid: e.currency
                                 for mid, e in self.store.movement_index.items()},
            withdrawn=set(self.store.withdrawn),
            has_rate=self.store.has_rate,
        )

    def ingest_text(self, text: str, *, fmt: str = "json", source: str = "",
                    batch_id: str = "", period: str = "") -> dict:
        """Ingest one raw file. A malformed file rejects its own batch only."""
        try:
            if fmt == "csv":
                doc = parser.parse_csv_text(text, source=source, batch_id=batch_id,
                                            period=period)
                content_hash = parser.canonical_hash(text)
            else:
                doc = parser.parse_json_text(text)
                # hash the canonical form so reformatting the same file still
                # counts as the same batch
                content_hash = parser.canonical_hash(json.dumps(
                    doc, sort_keys=True, ensure_ascii=False, default=str))
        except parser.BatchRejected as exc:
            result = BatchResult(batch_id=batch_id or None, source=source or None,
                                 period=period or None, status="rejected",
                                 reason=str(exc),
                                 content_hash=parser.canonical_hash(text)[:16])
            self.store.rejected_attempts.append(result.as_dict())
            return result.as_dict()
        return self.ingest(doc, content_hash=content_hash)

    def ingest(self, doc: dict, *, content_hash: Optional[str] = None) -> dict:
        if content_hash is None:
            content_hash = parser.canonical_hash(
                json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str))
        try:
            validated = parser.validate_batch(doc, self._known_view())
        except parser.BatchRejected as exc:
            meta = doc.get("batch") if isinstance(doc, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            result = BatchResult(
                batch_id=meta.get("batch_id"), source=meta.get("source"),
                period=meta.get("period"), status="rejected", reason=str(exc),
                content_hash=content_hash[:16])
            self.store.rejected_attempts.append(result.as_dict())
            return result.as_dict()

        batch_id = validated.batch_id
        prior = self.store.batches.get(batch_id)
        if prior is not None:
            if prior["content_hash"] == content_hash[:16]:
                dup = dict(prior)
                dup["status"] = "duplicate"
                return dup
            result = BatchResult(
                batch_id=batch_id, source=validated.source, period=validated.period,
                status="rejected",
                reason="batch_id re-used with different content",
                content_hash=content_hash[:16])
            self.store.rejected_attempts.append(result.as_dict())
            return result.as_dict()

        last_closed = max(self.store.closes) if self.store.closes else None
        if last_closed is not None and validated.period <= last_closed:
            result = BatchResult(
                batch_id=batch_id, source=validated.source, period=validated.period,
                status="rejected",
                reason=f"booking period {validated.period} is already closed "
                       f"(last closed {last_closed}); rebook in an open period",
                content_hash=content_hash[:16])
            self.store.rejected_attempts.append(result.as_dict())
            return result.as_dict()

        for risk in validated.risks:
            self.store.risks.setdefault(risk.risk_id, risk)
        for claim in validated.claims:
            self.store.claims.setdefault(claim.claim_id, claim)

        confirmed = duplicates = 0
        # additions and recoveries first (row order), then corrections and
        # withdrawals (row order), so out-of-order rows inside one file resolve
        additions = [m for m in validated.movements if m.type in ("ADDITION", "RECOVERY")]
        dependents = [m for m in validated.movements if m.type in ("CORRECTION", "WITHDRAWAL")]
        for pm in additions + dependents:
            if pm.duplicate:
                duplicates += 1
                continue
            self._confirm_movement(validated, pm)
            confirmed += 1

        if validated.errors:
            status = "partial" if confirmed or duplicates else "rejected"
        else:
            status = "confirmed"
        result = BatchResult(
            batch_id=batch_id, source=validated.source, period=validated.period,
            status=status, received_rows=validated.raw_movement_count,
            confirmed=confirmed, duplicates=duplicates,
            content_hash=content_hash[:16], errors=list(validated.errors))
        self.store.batches[batch_id] = result.as_dict()
        for err in validated.errors:
            self.store.quarantine.append({
                "batch_id": batch_id, "source": validated.source,
                "period": validated.period, **err.as_dict()})
        return result.as_dict()

    def _confirm_movement(self, batch, pm) -> None:
        store = self.store
        claim = store.claims[pm.claim_id]
        risk = store.risks[claim.risk_id]
        settlement = store.programs[risk.treaty_program].settlement_currency

        fx_snapshot_id = fx_revision = fx_rate = None
        recovery = ZERO
        if pm.type == "ADDITION":
            delta = pm.amount
            fx_snapshot_id, fx_revision, fx_rate = store.fx_rate(pm.currency, batch.period)
            contribution = q_money(delta * fx_rate)
        elif pm.type == "RECOVERY":
            delta = -pm.amount
            fx_snapshot_id, fx_revision, fx_rate = store.fx_rate(pm.currency, batch.period)
            converted = q_money(pm.amount * fx_rate)
            contribution = -converted
            recovery = converted
        elif pm.type == "CORRECTION":
            previous = store.effective_amount(pm.target_movement_id)
            delta = pm.amount - previous
            fx_snapshot_id, fx_revision, fx_rate = store.fx_rate(pm.currency, batch.period)
            contribution = q_money(delta * fx_rate)
        else:  # WITHDRAWAL reverses the target's frozen settlement amounts
            delta = -store.effective_amount(pm.target_movement_id)
            contribution = -store.effective_contribution(pm.target_movement_id)

        store.add_event(MovementEvent(
            event_seq=store.next_seq(),
            movement_id=pm.movement_id,
            claim_id=pm.claim_id,
            type=pm.type,
            amount=pm.amount,
            currency=pm.currency,
            delta_amount=delta,
            contribution=contribution,
            recovery=recovery,
            fx_snapshot_id=fx_snapshot_id if pm.currency != settlement else None,
            fx_revision=fx_revision if pm.currency != settlement else None,
            fx_rate=fx_rate if pm.currency != settlement else None,
            booking_period=batch.period,
            occurrence_period=pm.occurrence_period,
            target_movement_id=pm.target_movement_id,
            batch_id=batch.batch_id,
            source=batch.source,
            row=pm.row,
        ))

    # --------------------------------------------------------------- closes
    def close_all(self) -> list:
        """Close every unclosed booking period from the first to the last
        period with events, filling empty months with zero-movement closes."""
        if not self.store.events:
            return []
        periods = sorted({e.booking_period for e in self.store.events})
        new_closes = []
        for period in periods_between(periods[0], periods[-1]):
            if period in self.store.closes:
                continue
            prev = self.store.closes.get(prev_period(period))
            close = engine.compute_close(
                events=self.store.events, claims=self.store.claims,
                risks=self.store.risks, programs=self.store.programs,
                period=period, prev_close=prev)
            self.store.closes[period] = close
            new_closes.append(close)
        return new_closes

    def rebuild(self) -> dict:
        """Replay the event log from scratch and verify every stored close."""
        if not self.store.events:
            return {"consistent": True, "periods": {}, "event_count": 0,
                    "detail": "no events"}
        periods = sorted({e.booking_period for e in self.store.events})
        recomputed = {}
        prev = None
        for period in periods_between(periods[0], periods[-1]):
            close = engine.compute_close(
                events=self.store.events, claims=self.store.claims,
                risks=self.store.risks, programs=self.store.programs,
                period=period, prev_close=prev)
            recomputed[period] = close
            prev = close
        report = {}
        consistent = True
        for period in sorted(set(recomputed) | set(self.store.closes)):
            stored = self.store.closes.get(period)
            again = recomputed.get(period)
            if stored is None:
                status, consistent = "missing_stored", False
            elif again is None:
                status, consistent = "missing_rebuild", False
            else:
                same = (json.dumps(json_ready(stored), sort_keys=True)
                        == json.dumps(json_ready(again), sort_keys=True))
                status = "match" if same else "mismatch"
                consistent = consistent and same
            report[period] = status
        return {"consistent": consistent, "periods": report,
                "event_count": len(self.store.events)}

    def replay(self, period: str, *, program: Optional[str] = None,
               version: Optional[int] = None) -> dict:
        """Recompute a closed period, optionally under a pinned treaty version.

        The replay is never persisted: stored closes are immutable.
        """
        if period not in self.store.closes:
            return {"status": "error",
                    "reason": f"period {period} is not closed; nothing to replay"}
        override = None
        if program is not None:
            if version is None:
                return {"status": "error",
                        "reason": "--treaty-version is required with --program"}
            override = {program: version}
        prev = self.store.closes.get(prev_period(period))
        close = engine.compute_close(
            events=self.store.events, claims=self.store.claims,
            risks=self.store.risks, programs=self.store.programs,
            period=period, prev_close=prev, version_override=override)
        return json_ready(close)

    # --------------------------------------------------------------- report
    def report(self) -> dict:
        store = self.store
        data = {
            "settlement_currency": store.settlement_currency,
            "event_count": len(store.events),
            "periods": [store.closes[p] for p in sorted(store.closes)],
            "batches": [store.batches[k] for k in sorted(store.batches)],
            "rejected_attempts": list(store.rejected_attempts),
            "quarantine": list(store.quarantine),
            "programs": [_program_summary(store.program_docs[k])
                         for k in sorted(store.program_docs)],
            "fx_snapshots": [_fx_summary(d) for d in store.fx_docs],
        }
        return build_result(PROJECT, json_ready(data))


def _program_summary(doc: dict) -> dict:
    return {
        "program_id": doc["program_id"],
        "settlement_currency": doc["settlement_currency"],
        "versions": [
            {"version": v["version"], "effective_from": v["effective_from"],
             "effective_to": v.get("effective_to"),
             "layers": [
                 {"layer_id": l["layer_id"], "attachment": str(l["attachment"]),
                  "limit": str(l["limit"]),
                  "reinstatements": l.get("reinstatements", 0),
                  "reinstatement_premium_rate": str(l.get("reinstatement_premium_rate", "0"))}
                 for l in v["layers"]]}
            for v in doc["versions"]],
    }


def _fx_summary(doc: dict) -> dict:
    return {
        "snapshot_id": doc["snapshot_id"],
        "period": doc["period"],
        "revision": doc.get("revision", 1),
        "base": doc["base"],
        "rates": {ccy: str(rate) for ccy, rate in sorted(doc["rates"].items())},
    }


def apply_ops(pipeline: Pipeline, ops: list) -> None:
    for kind, doc in ops:
        if kind == "reference":
            pipeline.register_reference(doc)
        else:
            pipeline.ingest(doc)


def run(payload: dict) -> dict:
    """Process a bundle payload end-to-end and return the full report."""
    pipeline = Pipeline()
    apply_ops(pipeline, parser.split_bundle(payload))
    pipeline.close_all()
    report = pipeline.report()
    report["rebuild"] = pipeline.rebuild()
    return report
