"""Input validation and record extraction for JSON and CSV bordereaux.

Every row is validated independently: a bad row is quarantined with a reason
and never blocks the confirmation of the good rows in the same batch. This
module is pure validation — FX pinning and delta computation happen in the
pipeline, where the event log is available.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from .models import (
    MOVEMENT_TYPES,
    Claim,
    DomainError,
    FxSnapshot,
    Layer,
    Risk,
    RowError,
    TreatyProgram,
    TreatyVersion,
    is_currency,
    is_period,
    movement_fingerprint,
    q_rate,
    to_decimal,
)


class BatchRejected(Exception):
    """Batch-level failure: nothing in the batch can be confirmed."""


# ---------------------------------------------------------------------------
# canonical hashing / payload shapes
# ---------------------------------------------------------------------------

def canonical_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatchRejected(f"invalid JSON: {exc}")


def split_bundle(doc: Any) -> list:
    """Split a payload into an ordered list of ("reference"|"batch", doc) ops.

    Accepted shapes:
      - steps:      {"steps": [{"reference": {...}} | {"batch": ...}, ...]}
                    (ops are applied in order, so reference data such as an FX
                    revision can arrive between batches)
      - bundle:     {"reference": {...}, "batches": [...]} (reference first)
      - batch:      {"batch": {...}, "movements": [...]}
      - reference:  {"programs": [...]} and/or {"fx": [...]}
    """
    if not isinstance(doc, dict):
        raise BatchRejected("top-level payload must be a JSON object")
    if "steps" in doc:
        steps = doc["steps"]
        if not isinstance(steps, list):
            raise BatchRejected("steps must be a list")
        ops = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise BatchRejected(f"steps[{i}] must be an object")
            if "reference" in step:
                ops.append(("reference", step["reference"]))
            elif "batch" in step:
                ops.append(("batch", step))
            else:
                raise BatchRejected(f"steps[{i}]: expected 'reference' or 'batch'")
        return ops
    if "reference" in doc or "batches" in doc:
        reference = doc.get("reference") or {}
        batches = doc.get("batches") or []
        if not isinstance(reference, dict):
            raise BatchRejected("reference must be an object")
        if not isinstance(batches, list):
            raise BatchRejected("batches must be a list")
        ops = [("reference", reference)] if reference else []
        return ops + [("batch", b) for b in batches]
    if "batch" in doc:
        return [("batch", doc)]
    if "programs" in doc or "fx" in doc:
        return [("reference", doc)]
    raise BatchRejected("unrecognized payload: expected reference, batch, or bundle")


# ---------------------------------------------------------------------------
# reference data parsing
# ---------------------------------------------------------------------------

def layer_from_doc(doc: dict, where: str) -> Layer:
    try:
        layer_id = str(doc["layer_id"])
        attachment = to_decimal(doc["attachment"], f"{where}.attachment")
        limit = to_decimal(doc["limit"], f"{where}.limit")
    except KeyError as exc:
        raise DomainError(f"{where}: missing field {exc}")
    reinstatements = doc.get("reinstatements", 0)
    if isinstance(reinstatements, bool) or not isinstance(reinstatements, int) or reinstatements < 0:
        raise DomainError(f"{where}.reinstatements: must be a non-negative integer")
    rate = to_decimal(doc.get("reinstatement_premium_rate", "0"), f"{where}.reinstatement_premium_rate")
    if attachment < 0:
        raise DomainError(f"{where}.attachment: must be >= 0")
    if limit <= 0:
        raise DomainError(f"{where}.limit: must be > 0")
    if rate < 0:
        raise DomainError(f"{where}.reinstatement_premium_rate: must be >= 0")
    return Layer(layer_id=layer_id, attachment=attachment, limit=limit,
                 reinstatements=reinstatements, reinstatement_premium_rate=rate)


def program_from_doc(doc: dict) -> TreatyProgram:
    if not isinstance(doc, dict):
        raise DomainError("program must be an object")
    program_id = doc.get("program_id")
    if not program_id or not isinstance(program_id, str):
        raise DomainError("program_id: required string")
    settlement = doc.get("settlement_currency")
    if not is_currency(settlement):
        raise DomainError(f"settlement_currency: invalid currency {settlement!r}")
    raw_versions = doc.get("versions")
    if not isinstance(raw_versions, list) or not raw_versions:
        raise DomainError("versions: must be a non-empty list")

    versions = []
    seen_ids = set()
    for i, vd in enumerate(raw_versions):
        where = f"versions[{i}]"
        if not isinstance(vd, dict):
            raise DomainError(f"{where}: must be an object")
        version_no = vd.get("version")
        if isinstance(version_no, bool) or not isinstance(version_no, int) or version_no < 1:
            raise DomainError(f"{where}.version: must be a positive integer")
        if version_no in seen_ids:
            raise DomainError(f"{where}.version: duplicate version {version_no}")
        seen_ids.add(version_no)
        eff_from = vd.get("effective_from")
        eff_to = vd.get("effective_to")
        if not is_period(eff_from):
            raise DomainError(f"{where}.effective_from: invalid period {eff_from!r}")
        if eff_to is not None and not is_period(eff_to):
            raise DomainError(f"{where}.effective_to: invalid period {eff_to!r}")
        if eff_to is not None and eff_to < eff_from:
            raise DomainError(f"{where}: effective_to before effective_from")
        raw_layers = vd.get("layers")
        if not isinstance(raw_layers, list) or not raw_layers:
            raise DomainError(f"{where}.layers: must be a non-empty list")
        layers = tuple(sorted(
            (layer_from_doc(ld, f"{where}.layers[{j}]") for j, ld in enumerate(raw_layers)),
            key=lambda l: (l.attachment, l.layer_id),
        ))
        layer_ids = [l.layer_id for l in layers]
        if len(set(layer_ids)) != len(layer_ids):
            raise DomainError(f"{where}.layers: duplicate layer_id")
        for a, b in zip(layers, layers[1:]):
            if b.attachment < a.exhaustion:
                raise DomainError(
                    f"{where}.layers: layer {b.layer_id} attaches at {b.attachment} "
                    f"below the exhaustion {a.exhaustion} of {a.layer_id}; "
                    f"overlapping layers are not supported"
                )
        versions.append(TreatyVersion(
            program=program_id, version=version_no,
            effective_from=eff_from, effective_to=eff_to, layers=layers,
        ))

    versions.sort(key=lambda v: v.effective_from)
    for a, b in zip(versions, versions[1:]):
        if a.effective_to is None or b.effective_from <= a.effective_to:
            raise DomainError(
                f"treaty versions {a.version} and {b.version} overlap in their "
                f"effective ranges; a version may only govern its own range"
            )
    return TreatyProgram(program_id=program_id, settlement_currency=settlement,
                         versions=tuple(versions))


def fx_from_doc(doc: dict) -> FxSnapshot:
    if not isinstance(doc, dict):
        raise DomainError("fx snapshot must be an object")
    snapshot_id = doc.get("snapshot_id")
    if not snapshot_id or not isinstance(snapshot_id, str):
        raise DomainError("snapshot_id: required string")
    period = doc.get("period")
    if not is_period(period):
        raise DomainError(f"period: invalid period {period!r}")
    revision = doc.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise DomainError("revision: must be a positive integer")
    base = doc.get("base")
    if not is_currency(base):
        raise DomainError(f"base: invalid currency {base!r}")
    raw_rates = doc.get("rates")
    if not isinstance(raw_rates, dict):
        raise DomainError("rates: must be an object mapping currency to rate")
    rates = {}
    for ccy, raw in raw_rates.items():
        if not is_currency(ccy):
            raise DomainError(f"rates: invalid currency {ccy!r}")
        rate = q_rate(to_decimal(raw, f"rates.{ccy}"))
        if rate <= 0:
            raise DomainError(f"rates.{ccy}: must be > 0")
        if ccy != base:
            rates[ccy] = rate
    return FxSnapshot(snapshot_id=snapshot_id, period=period, revision=revision,
                      base=base, rates=rates)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CSV_REQUIRED_COLUMNS = {"movement_id", "claim_id", "type", "amount", "currency"}


def parse_csv_text(text: str, *, source: str, batch_id: str, period: str) -> dict:
    """Parse a CSV bordereau into a batch doc.

    Rows may carry ``risk_id``/``treaty_program`` (plus optional ``policy_id``
    and ``cedent``) to register the underlying risk and claim implicitly.
    """
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = CSV_REQUIRED_COLUMNS - fieldnames
    if missing:
        raise BatchRejected(f"csv missing required columns: {sorted(missing)}")
    movements = []
    for line_no, row in enumerate(reader, start=2):  # header is line 1
        cleaned = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        cleaned.pop(None, None)  # stray extra cells
        cleaned["_row"] = line_no
        movements.append(cleaned)
    return {
        "batch": {"source": source, "batch_id": batch_id, "period": period},
        "movements": movements,
    }


# ---------------------------------------------------------------------------
# batch validation
# ---------------------------------------------------------------------------

@dataclass
class KnownView:
    """Read-only view of already-confirmed state, used to validate a batch."""
    programs: set
    risks: dict                       # risk_id -> Risk
    claims: dict                      # claim_id -> Claim
    movement_fingerprints: dict       # movement_id -> fingerprint tuple
    movement_types: dict              # movement_id -> movement type
    movement_claims: dict             # movement_id -> claim_id
    movement_currencies: dict         # movement_id -> currency
    withdrawn: set                    # movement_ids already withdrawn
    has_rate: Callable[[str, str], bool]


@dataclass
class PendingMovement:
    row: int
    movement_id: str
    claim_id: str
    type: str
    amount: Decimal
    currency: str
    occurrence_period: Optional[str]
    target_movement_id: Optional[str]
    duplicate: bool = False


@dataclass
class ValidatedBatch:
    source: str
    batch_id: str
    period: str
    risks: list = field(default_factory=list)        # list[Risk] to register
    claims: list = field(default_factory=list)       # list[Claim] to register
    movements: list = field(default_factory=list)    # list[PendingMovement]
    errors: list = field(default_factory=list)       # list[RowError]
    raw_movement_count: int = 0


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field_name}: required non-empty string")
    return value.strip()


def validate_batch(doc: dict, known: KnownView) -> ValidatedBatch:
    if not isinstance(doc, dict):
        raise BatchRejected("batch document must be an object")
    meta = doc.get("batch")
    if not isinstance(meta, dict):
        raise BatchRejected("batch: missing metadata object")
    try:
        source = _require_str(meta.get("source"), "batch.source")
        batch_id = _require_str(meta.get("batch_id"), "batch.batch_id")
    except DomainError as exc:
        raise BatchRejected(str(exc))
    period = meta.get("period")
    if not is_period(period):
        raise BatchRejected(f"batch.period: invalid period {period!r}")

    out = ValidatedBatch(source=source, batch_id=batch_id, period=period)

    def section(name: str) -> list:
        raw = doc.get(name, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise BatchRejected(f"{name}: must be a list")
        return raw

    # ---- risks -----------------------------------------------------------
    batch_risks: dict = {}
    for i, rd in enumerate(section("risks")):
        row, ident = i, "<risk>"
        try:
            if not isinstance(rd, dict):
                raise DomainError("risk row must be an object")
            risk_id = _require_str(rd.get("risk_id"), "risk_id")
            ident = risk_id
            program = _require_str(rd.get("treaty_program"), "treaty_program")
            if program not in known.programs:
                raise DomainError(f"treaty_program: unknown program {program!r}")
            currency = rd.get("currency", "")
            if not is_currency(currency):
                raise DomainError(f"currency: invalid currency {currency!r}")
            risk = Risk(risk_id=risk_id, treaty_program=program, currency=currency,
                        policy_id=str(rd.get("policy_id") or ""),
                        cedent=str(rd.get("cedent") or ""))
            existing = known.risks.get(risk_id) or batch_risks.get(risk_id)
            if existing is not None and existing != risk:
                raise DomainError(f"risk_id {risk_id!r} re-registered with different content")
            if risk_id not in batch_risks and risk_id not in known.risks:
                batch_risks[risk_id] = risk
                out.risks.append(risk)
        except DomainError as exc:
            out.errors.append(RowError(row=row, identifier=ident, reason=str(exc)))

    # ---- claims ----------------------------------------------------------
    batch_claims: dict = {}
    for i, cd in enumerate(section("claims")):
        row, ident = i, "<claim>"
        try:
            if not isinstance(cd, dict):
                raise DomainError("claim row must be an object")
            claim_id = _require_str(cd.get("claim_id"), "claim_id")
            ident = claim_id
            risk_id = _require_str(cd.get("risk_id"), "risk_id")
            if risk_id not in known.risks and risk_id not in batch_risks:
                raise DomainError(f"risk_id {risk_id!r} not registered")
            claim = Claim(claim_id=claim_id, risk_id=risk_id)
            existing = known.claims.get(claim_id) or batch_claims.get(claim_id)
            if existing is not None and existing != claim:
                raise DomainError(f"claim_id {claim_id!r} re-registered with different content")
            if claim_id not in batch_claims and claim_id not in known.claims:
                batch_claims[claim_id] = claim
                out.claims.append(claim)
        except DomainError as exc:
            out.errors.append(RowError(row=row, identifier=ident, reason=str(exc)))

    # ---- movements -------------------------------------------------------
    raw_movements = section("movements")
    out.raw_movement_count = len(raw_movements)

    available_claims = dict(known.claims)
    available_claims.update(batch_claims)

    # first pass: parse rows, collect batch movement ids for reference resolution
    parsed: list = []   # (row, dict) rows that parsed far enough to have an id
    batch_ids: dict = {}  # movement_id -> (row, type, claim_id, currency)
    for i, md in enumerate(raw_movements):
        if not isinstance(md, dict):
            out.errors.append(RowError(row=i, identifier="<movement>",
                                       reason="movement row must be an object"))
            continue
        row = md.get("_row", i)
        mid = md.get("movement_id")
        if isinstance(mid, str) and mid.strip():
            mid = mid.strip()
            if mid in batch_ids:
                out.errors.append(RowError(row=row, identifier=mid,
                                           reason="duplicate movement_id within batch"))
                continue
            batch_ids[mid] = (row, str(md.get("type") or "").strip().upper(),
                              str(md.get("claim_id") or "").strip(),
                              str(md.get("currency") or "").strip().upper())
        parsed.append((row, md))

    batch_withdrawn: set = set()
    for row, md in parsed:
        ident = str(md.get("movement_id") or "<movement>")
        try:
            movement_id = _require_str(md.get("movement_id"), "movement_id")
            ident = movement_id
            mtype = _require_str(md.get("type"), "type").upper()
            if mtype not in MOVEMENT_TYPES:
                raise DomainError(f"unknown movement type {mtype!r}")

            # implicit risk/claim registration from movement-row fields
            claim_id = _require_str(md.get("claim_id"), "claim_id")
            risk_id = (md.get("risk_id") or "").strip() if isinstance(md.get("risk_id"), str) else md.get("risk_id")
            if claim_id not in available_claims:
                if not risk_id or not isinstance(risk_id, str):
                    raise DomainError(f"claim_id {claim_id!r} not registered")
                program = md.get("treaty_program")
                if not isinstance(program, str) or program not in known.programs:
                    raise DomainError(f"treaty_program: unknown program {program!r}")
                if risk_id not in known.risks and risk_id not in batch_risks:
                    risk = Risk(risk_id=risk_id, treaty_program=program,
                                currency=str(md.get("currency") or ""),
                                    policy_id=str(md.get("policy_id") or ""),
                                    cedent=str(md.get("cedent") or ""))
                    batch_risks[risk_id] = risk
                    out.risks.append(risk)
                claim = Claim(claim_id=claim_id, risk_id=risk_id)
                batch_claims[claim_id] = claim
                available_claims[claim_id] = claim
                out.claims.append(claim)

            target = md.get("target_movement_id") or None
            if isinstance(target, str):
                target = target.strip() or None
            if mtype in ("WITHDRAWAL", "CORRECTION"):
                if not target:
                    raise DomainError(f"target_movement_id: required for {mtype}")
                if target == movement_id:
                    raise DomainError("movement cannot target itself")
                if target in known.movement_types:
                    target_type = known.movement_types[target]
                    target_claim = known.movement_claims.get(target)
                    target_currency = known.movement_currencies.get(target)
                elif target in batch_ids:
                    _, target_type, target_claim, target_currency = batch_ids[target]
                else:
                    raise DomainError(f"target_movement_id {target!r} not found")
                if target_type != "ADDITION":
                    raise DomainError(f"{mtype} target {target!r} must be an ADDITION")
                if target_claim != claim_id:
                    raise DomainError(f"{mtype} target {target!r} belongs to another claim")
                if target in known.withdrawn or target in batch_withdrawn:
                    raise DomainError(f"target {target!r} already withdrawn")
            elif target:
                raise DomainError(f"target_movement_id not allowed for {mtype}")
            else:
                target_currency = None

            raw_currency = md.get("currency")
            if (raw_currency is None or str(raw_currency).strip() == "") \
                    and mtype == "WITHDRAWAL" and target_currency:
                # a withdrawal converts nothing; inherit the target's currency
                currency = target_currency
            else:
                currency = _require_str(raw_currency, "currency").upper()
            if not is_currency(currency):
                raise DomainError(f"currency: invalid currency {currency!r}")
            if mtype != "WITHDRAWAL" and not known.has_rate(currency, out.period):
                raise DomainError(
                    f"no FX rate for {currency} in booking period {out.period}")

            occurrence = md.get("occurrence_period") or None
            if occurrence is not None and not is_period(occurrence):
                raise DomainError(f"occurrence_period: invalid period {occurrence!r}")

            raw_amount = md.get("amount")
            if mtype == "WITHDRAWAL" and (raw_amount is None or raw_amount == ""):
                amount = Decimal("0.00")
            else:
                try:
                    amount = to_decimal(raw_amount, "amount")
                except DomainError:
                    raise DomainError(f"amount: not a number: {raw_amount!r}")
            if mtype in ("ADDITION", "RECOVERY") and amount <= 0:
                raise DomainError("amount: must be > 0")
            if mtype == "CORRECTION" and amount < 0:
                raise DomainError("amount: corrected amount must be >= 0")

            if mtype == "WITHDRAWAL":
                batch_withdrawn.add(target)

            duplicate = False
            fingerprint = movement_fingerprint({
                "claim_id": claim_id, "type": mtype, "amount": amount,
                "currency": currency, "target_movement_id": target,
                "occurrence_period": occurrence,
            })
            existing_fp = known.movement_fingerprints.get(movement_id)
            if existing_fp is not None:
                if existing_fp == fingerprint:
                    duplicate = True
                else:
                    raise DomainError(
                        f"movement_id {movement_id!r} already booked with different "
                        f"content; submit a CORRECTION movement instead")

            out.movements.append(PendingMovement(
                row=row, movement_id=movement_id, claim_id=claim_id, type=mtype,
                amount=amount, currency=currency, occurrence_period=occurrence,
                target_movement_id=target, duplicate=duplicate,
            ))
        except DomainError as exc:
            out.errors.append(RowError(row=row, identifier=ident, reason=str(exc)))

    return out
