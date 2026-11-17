"""Domain calculation only: cumulative claim positions, treaty layer
allocation, reinstatements, recoveries, and the monthly period close.

The engine never looks up FX rates and never performs I/O. Confirmed events
already carry frozen settlement-currency contributions, so a close is a pure
function of (events, reference data, period, previous close).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Optional

from .models import (
    ZERO,
    MovementEvent,
    TreatyProgram,
    q_money,
    rate_str,
    to_decimal,
)


def claim_positions(events) -> dict:
    """claim_id -> cumulative ground-up loss / recoveries in settlement ccy."""
    positions = {}
    for e in events:
        p = positions.get(e.claim_id)
        if p is None:
            p = positions[e.claim_id] = {
                "ground_up": ZERO, "recoveries": ZERO,
                "currency": e.currency, "first_seq": e.event_seq,
            }
        p["ground_up"] += e.contribution
        p["recoveries"] += e.recovery
    return positions


class _Prev:
    """Decimal-safe view over the previous period's close (stored closes may
    have been round-tripped through JSON, so values can be strings)."""

    def __init__(self, prev_close: Optional[dict]):
        prev_close = prev_close or {}
        self.claims = {c["claim_id"]: c for c in prev_close.get("claims", [])}
        self.programs = {p["program"]: p for p in prev_close.get("programs", [])}

    def claim_ground(self, claim_id: str) -> Decimal:
        c = self.claims.get(claim_id)
        return to_decimal(c["ground_up"]["cumulative"]) if c else ZERO

    def claim_recoveries(self, claim_id: str) -> Decimal:
        c = self.claims.get(claim_id)
        return to_decimal(c["recoveries"]["cumulative"]) if c else ZERO

    def claim_layer_ids(self, claim_id: str) -> list:
        c = self.claims.get(claim_id)
        return [l["layer_id"] for l in c["layers"]] if c else []

    def claim_layer_ceded(self, claim_id: str, layer_id: str) -> Decimal:
        c = self.claims.get(claim_id)
        if not c:
            return ZERO
        for l in c["layers"]:
            if l["layer_id"] == layer_id:
                return to_decimal(l["ceded"]["cumulative"])
        return ZERO

    def program_layer_ids(self, program: str) -> list:
        p = self.programs.get(program)
        return [l["layer_id"] for l in p.get("layers", [])] if p else []

    def program_layer_value(self, program: str, layer_id: str, key: str) -> Decimal:
        p = self.programs.get(program)
        if not p:
            return ZERO
        for l in p.get("layers", []):
            if l["layer_id"] == layer_id:
                return to_decimal(l[key]["cumulative"])
        return ZERO


def _allocate_program(version, order, positions):
    """Per-claim layer cession with aggregate capacity and reinstatements.

    Returns (precap, allowed, layer_meta). The aggregate cap is distributed to
    claims in deterministic first-reported order: earliest claims erode the
    layer aggregate first.
    """
    layers = version.layers  # already sorted by attachment
    precap = {}
    for cid in order:
        basis = positions[cid]["ground_up"]
        if basis < 0:
            basis = ZERO
        precap[cid] = {
            l.layer_id: min(max(basis - l.attachment, ZERO), l.limit) for l in layers
        }
    allowed = {cid: {} for cid in order}
    layer_meta = {}
    for l in layers:
        gross = ZERO
        for cid in order:
            gross += precap[cid][l.layer_id]
        capacity = l.aggregate_capacity
        allowed_agg = min(gross, capacity)
        remaining = allowed_agg
        for cid in order:
            share = min(precap[cid][l.layer_id], remaining)
            allowed[cid][l.layer_id] = share
            remaining -= share
        reinstated = min(max(allowed_agg - l.limit, ZERO), l.limit * l.reinstatements)
        used = 0
        if reinstated > 0 and l.limit > 0:
            used = int((reinstated / l.limit).to_integral_value(rounding=ROUND_CEILING))
        layer_meta[l.layer_id] = {
            "layer": l,
            "gross": gross,
            "allowed": allowed_agg,
            "capacity": capacity,
            "reinstated": reinstated,
            "reinstatements_used": used,
            "premium_cum": q_money(l.reinstatement_premium_rate * reinstated),
        }
    return precap, allowed, layer_meta


def compute_close(*, events, claims, risks, programs, period,
                  prev_close=None, version_override=None) -> dict:
    """Compute the monthly close for ``period`` from all events booked up to
    and including it. ``version_override`` ({program_id: version}) replays the
    period under a pinned treaty version without touching stored closes.
    """
    version_override = version_override or {}
    prev = _Prev(prev_close)
    ev = [e for e in events if e.booking_period <= period]
    # canonical output order: independent of batch arrival order and of row
    # order inside any file
    period_events = sorted(
        (e for e in ev if e.booking_period == period),
        key=lambda e: (e.batch_id, e.movement_id, e.event_seq))
    positions = claim_positions(ev)

    versions_used: dict = {}
    program_errors: dict = {}
    program_state: dict = {}  # pid -> (layers, precap, allowed, layer_meta)

    for pid in sorted(programs):
        prog: TreatyProgram = programs[pid]
        if pid in version_override:
            version = prog.version_by_id(version_override[pid])
            if version is None:
                program_errors[pid] = f"treaty version {version_override[pid]} not found"
                continue
        else:
            version = prog.version_for(period)
            if version is None:
                program_errors[pid] = "no treaty version effective for this period"
                continue
        versions_used[pid] = version.version
        cids = [cid for cid in positions
                if cid in claims and risks[claims[cid].risk_id].treaty_program == pid]
        order = sorted(cids, key=lambda cid: (positions[cid]["first_seq"], cid))
        precap, allowed, layer_meta = _allocate_program(version, order, positions)
        program_state[pid] = (version.layers, precap, allowed, layer_meta)

    # ---- per-claim rows ---------------------------------------------------
    claim_rows = []
    tot = {k: ZERO for k in (
        "ground_mov", "ground_cum", "rec_mov", "rec_cum",
        "ceded_mov", "ceded_cum", "unalloc_cum", "basis_cum",
        "components_cum", "prem_mov", "prem_cum")}
    claims_with_errors = []

    for cid in sorted(positions):
        pos = positions[cid]
        cum = pos["ground_up"]
        rec = pos["recoveries"]
        basis = cum if cum > ZERO else ZERO
        errors = []
        if cum < ZERO:
            errors.append("negative cumulative loss floored to zero for allocation")
            claims_with_errors.append(cid)

        claim = claims.get(cid)
        pid = None
        layers = ()
        precap = allowed = {}
        if claim is not None:
            pid = risks[claim.risk_id].treaty_program
            state = program_state.get(pid)
            if state is not None:
                layers, precap_all, allowed_all, _ = state
                precap = precap_all.get(cid, {})
                allowed = allowed_all.get(cid, {})

        layer_rows = []
        ceded_cum_total = ZERO
        seen_layer_ids = set()
        for l in layers:
            lid = l.layer_id
            seen_layer_ids.add(lid)
            uncapped = precap.get(lid, ZERO)
            ceded = allowed.get(lid, ZERO)
            ceded_prev = prev.claim_layer_ceded(cid, lid)
            ceded_cum_total += ceded
            layer_rows.append({
                "layer_id": lid,
                "attachment": l.attachment,
                "limit": l.limit,
                "attachment_consumed": min(basis, l.attachment),
                "ceded": {"movement": ceded - ceded_prev, "cumulative": ceded},
                "ceded_uncapped": uncapped,
                "aggregate_shortfall": uncapped - ceded,
                "limit_remaining": l.limit - uncapped,
            })
        for lid in prev.claim_layer_ids(cid):
            if lid not in seen_layer_ids:
                ceded_prev = prev.claim_layer_ceded(cid, lid)
                layer_rows.append({
                    "layer_id": lid,
                    "removed_in_current_version": True,
                    "ceded": {"movement": -ceded_prev, "cumulative": ZERO},
                })
                # removed layers release their ceded amount back to unallocated
        ceded_mov_total = sum(
            (lr["ceded"]["movement"] for lr in layer_rows), ZERO)

        allowed_total = sum(allowed.values(), ZERO)
        if not layers:
            retention = gap = above_layer = above_aggregate = ZERO
            no_cover = basis
        else:
            retention = min(basis, layers[0].attachment)
            gap = ZERO
            for a, b in zip(layers, layers[1:]):
                if b.attachment > a.exhaustion:
                    gap += max(ZERO, min(basis, b.attachment) - min(basis, a.exhaustion))
            above_layer = max(ZERO, basis - layers[-1].exhaustion)
            above_aggregate = sum(precap.values(), ZERO) - allowed_total
            no_cover = ZERO
        unallocated_total = basis - allowed_total
        components = retention + gap + above_layer + above_aggregate + no_cover

        ground_prev = prev.claim_ground(cid)
        rec_prev = prev.claim_recoveries(cid)
        claim_rows.append({
            "claim_id": cid,
            "risk_id": claim.risk_id if claim else None,
            "program": pid,
            "currency": pos["currency"],
            "ground_up": {"movement": cum - ground_prev, "cumulative": cum},
            "recoveries": {"movement": rec - rec_prev, "cumulative": rec},
            "layers": layer_rows,
            "unallocated": {
                "retention": retention,
                "gap": gap,
                "above_layer": above_layer,
                "above_aggregate": above_aggregate,
                "no_cover": no_cover,
                "total": unallocated_total,
            },
            "errors": errors,
        })

        tot["ground_mov"] += cum - ground_prev
        tot["ground_cum"] += cum
        tot["rec_mov"] += rec - rec_prev
        tot["rec_cum"] += rec
        tot["ceded_mov"] += ceded_mov_total
        tot["ceded_cum"] += ceded_cum_total
        tot["unalloc_cum"] += unallocated_total
        tot["basis_cum"] += basis
        tot["components_cum"] += components

    # ---- per-program rows --------------------------------------------------
    program_rows = []
    for pid in sorted(programs):
        if pid in program_errors:
            program_rows.append({"program": pid, "error": program_errors[pid]})
            continue
        layers, _, _, layer_meta = program_state[pid]
        layer_rows = []
        seen = set()
        for l in layers:
            lid = l.layer_id
            seen.add(lid)
            m = layer_meta[lid]
            ceded_prev = prev.program_layer_value(pid, lid, "ceded")
            prem_prev = prev.program_layer_value(pid, lid, "reinstatement_premium")
            reinst_prev = prev.program_layer_value(pid, lid, "reinstated_amount")
            layer_rows.append({
                "layer_id": lid,
                "treaty_version": versions_used[pid],
                "attachment": m["layer"].attachment,
                "limit": m["layer"].limit,
                "aggregate_capacity": m["capacity"],
                "gross_ceded_cumulative": m["gross"],
                "ceded": {"movement": m["allowed"] - ceded_prev,
                          "cumulative": m["allowed"]},
                "reinstatements_available": m["layer"].reinstatements,
                "reinstatements_used": m["reinstatements_used"],
                "reinstated_amount": {"movement": m["reinstated"] - reinst_prev,
                                      "cumulative": m["reinstated"]},
                "reinstatement_premium": {"movement": m["premium_cum"] - prem_prev,
                                          "cumulative": m["premium_cum"]},
            })
            tot["prem_mov"] += m["premium_cum"] - prem_prev
            tot["prem_cum"] += m["premium_cum"]
        for lid in prev.program_layer_ids(pid):
            if lid not in seen:
                ceded_prev = prev.program_layer_value(pid, lid, "ceded")
                layer_rows.append({
                    "layer_id": lid,
                    "removed_in_current_version": True,
                    "ceded": {"movement": -ceded_prev, "cumulative": ZERO},
                })
        program_rows.append({
            "program": pid,
            "treaty_version": versions_used[pid],
            "layers": layer_rows,
        })

    # ---- movement detail for the period ------------------------------------
    movements = [{
        "movement_id": e.movement_id,
        "claim_id": e.claim_id,
        "type": e.type,
        "amount": e.amount,
        "currency": e.currency,
        "delta_amount": e.delta_amount,
        "contribution": e.contribution,
        "recovery": e.recovery,
        "fx": ({"snapshot_id": e.fx_snapshot_id, "revision": e.fx_revision,
                "rate": rate_str(e.fx_rate)} if e.fx_snapshot_id else None),
        "target_movement_id": e.target_movement_id,
        "batch_id": e.batch_id,
        "row": e.row,
    } for e in period_events]

    fx_used = sorted({f"{e.fx_snapshot_id}:{e.fx_revision}"
                      for e in period_events if e.fx_snapshot_id})

    conservation_delta = tot["basis_cum"] - (tot["ceded_cum"] + tot["components_cum"])
    close = {
        "period": period,
        "status": "closed",
        "treaty_versions": dict(sorted(versions_used.items())),
        "treaty_version_override": version_override or None,
        "replayed": bool(version_override),
        "fx_snapshots": fx_used,
        "event_count_cumulative": len(ev),
        "booking_events": len(period_events),
        "movements": movements,
        "claims": claim_rows,
        "programs": program_rows,
        "totals": {
            "ground_up": {"movement": tot["ground_mov"], "cumulative": tot["ground_cum"]},
            "recoveries": {"movement": tot["rec_mov"], "cumulative": tot["rec_cum"]},
            "ceded": {"movement": tot["ceded_mov"], "cumulative": tot["ceded_cum"]},
            "reinstatement_premium": {"movement": tot["prem_mov"],
                                      "cumulative": tot["prem_cum"]},
            "unallocated_cumulative": tot["unalloc_cum"],
            "conservation": "ok" if conservation_delta == 0 else "broken",
            "conservation_delta": conservation_delta,
        },
        "claims_with_errors": sorted(claims_with_errors),
    }
    return close
