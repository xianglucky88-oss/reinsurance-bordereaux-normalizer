"""End-to-end proof: per-claim per-layer results are explainable and totals
are conserved across layer crossings, cross-month corrections, FX revisions,
duplicate submissions, out-of-order rows, partial batches, treaty version
changes, and input permutations.
"""
import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from reinsurance_bordereaux_normalizer import parser
from reinsurance_bordereaux_normalizer.models import money_str as M
from reinsurance_bordereaux_normalizer.pipeline import Pipeline, run
from reinsurance_bordereaux_normalizer.report import dumps, json_ready

FIXTURES = Path(__file__).parents[1] / "fixtures"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def layer(lid, att, lim, rein=0, rate="0"):
    return {"layer_id": lid, "attachment": att, "limit": lim,
            "reinstatements": rein, "reinstatement_premium_rate": rate}


def version(v, frm, to, layers):
    return {"version": v, "effective_from": frm, "effective_to": to,
            "layers": layers}


def program(pid="P", versions=None, settlement="USD"):
    return {"program_id": pid, "settlement_currency": settlement,
            "versions": versions}


def fx(period, rates, sid=None, rev=1, base="USD"):
    return {"snapshot_id": sid or f"FX-{period}", "period": period,
            "revision": rev, "base": base, "rates": rates}


def risk(rid, prog="P", ccy="USD"):
    return {"risk_id": rid, "treaty_program": prog, "currency": ccy}


def claim(cid, rid):
    return {"claim_id": cid, "risk_id": rid}


def mv(mid, cid, type="ADDITION", amount=None, ccy="USD", target=None, occ=None):
    d = {"movement_id": mid, "claim_id": cid, "type": type, "currency": ccy}
    if amount is not None:
        d["amount"] = amount
    if target:
        d["target_movement_id"] = target
    if occ:
        d["occurrence_period"] = occ
    return d


def batch(bid, period, source="t", risks=(), claims=(), movements=()):
    return {"batch": {"source": source, "batch_id": bid, "period": period},
            "risks": list(risks), "claims": list(claims),
            "movements": list(movements)}


def make_pipeline(programs, fx_docs=()):
    p = Pipeline()
    result = p.register_reference({"programs": programs, "fx": list(fx_docs)})
    assert result["status"] == "ok", result
    return p


def claim_row(close, cid):
    return next(c for c in close["claims"] if c["claim_id"] == cid)


def clayer(crow, lid):
    return next(l for l in crow["layers"] if l["layer_id"] == lid)


def prog_layer(close, lid, pid="P"):
    prog = next(p for p in close["programs"] if p["program"] == pid)
    return next(l for l in prog["layers"] if l["layer_id"] == lid)


def close_of(pipeline, period):
    return pipeline.store.closes[period]


# ---------------------------------------------------------------------------
# 1. one claim crossing two layers, with conservation
# ---------------------------------------------------------------------------

class TestLayerCrossing(unittest.TestCase):
    def test_claim_crosses_attachment_into_two_layers(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50"), layer("L2", "150", "100")])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="120")]))
        jan = p.close_all()[0]

        c1 = claim_row(jan, "C1")
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "120.00")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "20.00")
        self.assertEqual(M(clayer(c1, "L1")["attachment_consumed"]), "100.00")
        self.assertEqual(M(clayer(c1, "L2")["ceded"]["cumulative"]), "0.00")
        self.assertEqual(M(c1["unallocated"]["retention"]), "100.00")
        self.assertEqual(M(c1["unallocated"]["total"]), "100.00")
        self.assertEqual(jan["totals"]["conservation"], "ok")

        p.ingest(batch("B2", "2026-02", movements=[mv("M2", "C1", amount="80")]))
        feb = p.close_all()[0]
        c1 = claim_row(feb, "C1")
        # cumulative 200: L1 full 50 (+30), L2 50 (+50); movement sums to the +80
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["movement"]), "30.00")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "50.00")
        self.assertEqual(M(clayer(c1, "L1")["limit_remaining"]), "0.00")
        self.assertEqual(M(clayer(c1, "L2")["ceded"]["movement"]), "50.00")
        self.assertEqual(M(c1["ground_up"]["movement"]), "80.00")
        layer_movements = sum(Decimal(M(l["ceded"]["movement"])) for l in c1["layers"])
        unalloc_movement = (Decimal(M(c1["unallocated"]["total"]))
                            - Decimal(M(claim_row(jan, "C1")["unallocated"]["total"])))
        self.assertEqual(layer_movements + unalloc_movement, Decimal("80.00"))
        self.assertEqual(feb["totals"]["conservation"], "ok")


# ---------------------------------------------------------------------------
# 2. corrections and withdrawals are not simple additions
# ---------------------------------------------------------------------------

class TestCorrectionAcrossMonths(unittest.TestCase):
    def test_correction_lands_in_reporting_month_and_keeps_history(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50"), layer("L2", "150", "100")])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="120")]))
        p.close_all()
        jan_before = dumps(close_of(p, "2026-01"))

        # February bordereau corrects M1 from 120 down to 90
        p.ingest(batch("B2", "2026-02",
                       movements=[mv("M2", "C1", type="CORRECTION", amount="90", target="M1")]))
        feb = p.close_all()[0]

        c1 = claim_row(feb, "C1")
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "90.00")
        self.assertEqual(M(c1["ground_up"]["movement"]), "-30.00")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "0.00")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["movement"]), "-20.00")
        self.assertEqual(M(c1["unallocated"]["total"]), "90.00")
        # the correction event carries the delta, not a new 90 on top of 120
        corr = next(m for m in feb["movements"] if m["movement_id"] == "M2")
        self.assertEqual(M(corr["delta_amount"]), "-30.00")
        self.assertEqual(M(corr["contribution"]), "-30.00")
        # January's close is immutable history
        self.assertEqual(dumps(close_of(p, "2026-01")), jan_before)
        self.assertTrue(p.rebuild()["consistent"])

    def test_withdrawal_reverses_at_frozen_values(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="120")]))
        p.close_all()
        p.ingest(batch("B2", "2026-02",
                       movements=[mv("M2", "C1", type="WITHDRAWAL", target="M1")]))
        feb = p.close_all()[0]
        c1 = claim_row(feb, "C1")
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "0.00")
        self.assertEqual(M(c1["ground_up"]["movement"]), "-120.00")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["movement"]), "-20.00")
        self.assertEqual(feb["totals"]["conservation"], "ok")
        # a second withdrawal of the same target is rejected
        again = p.ingest(batch("B3", "2026-03",
                               movements=[mv("M3", "C1", type="WITHDRAWAL", target="M1")]))
        self.assertEqual(again["status"], "rejected")
        self.assertIn("already withdrawn", again["errors"][0]["reason"])

    def test_withdrawal_of_corrected_movement_reverses_frozen_contributions(self):
        p = make_pipeline(
            [program(versions=[version(1, "2026-01", None, [
                layer("L1", "0", "1000")])])],
            fx_docs=[fx("2026-01", {"EUR": "1.10"}, sid="FX1"),
                     fx("2026-02", {"EUR": "1.20"}, sid="FX2")])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1", ccy="EUR")],
                       claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="100", ccy="EUR")]))
        # correction converts only the delta at the February rate
        p.ingest(batch("B2", "2026-02",
                       movements=[mv("M2", "C1", type="CORRECTION", amount="150",
                                     ccy="EUR", target="M1")]))
        # withdrawal needs no amount or currency and reverses the frozen
        # settlement contributions exactly: 100x1.10 + 50x1.20 = 170
        bare_withdrawal = {"movement_id": "M3", "claim_id": "C1",
                           "type": "WITHDRAWAL", "target_movement_id": "M1"}
        result = p.ingest(batch("B3", "2026-03", movements=[bare_withdrawal]))
        self.assertEqual(result["status"], "confirmed")
        closes = p.close_all()
        jan, feb, mar = closes[0], closes[1], closes[2]
        self.assertEqual(M(claim_row(jan, "C1")["ground_up"]["cumulative"]), "110.00")
        self.assertEqual(M(claim_row(feb, "C1")["ground_up"]["cumulative"]), "170.00")
        c1 = claim_row(mar, "C1")
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "0.00")
        self.assertEqual(M(c1["ground_up"]["movement"]), "-170.00")
        w = next(m for m in mar["movements"] if m["movement_id"] == "M3")
        self.assertEqual(M(w["contribution"]), "-170.00")
        self.assertIsNone(w["fx"])
        self.assertEqual(w["currency"], "EUR")  # inherited from the target
        self.assertEqual(mar["totals"]["conservation"], "ok")
        self.assertTrue(p.rebuild()["consistent"])


# ---------------------------------------------------------------------------
# 3. recoveries flow back through layers; reinstatements are counted
# ---------------------------------------------------------------------------

class TestRecoveryAndReinstatement(unittest.TestCase):
    def test_recovery_and_reinstatement_premium(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "0", "100", rein=1, rate="1.0")])])])
        p.ingest(batch("B1", "2026-01",
                       risks=[risk("RA")], claims=[claim("A", "RA")],
                       movements=[mv("M1", "A", amount="150")]))
        jan = p.close_all()[0]
        a = claim_row(jan, "A")
        self.assertEqual(M(clayer(a, "L1")["ceded"]["cumulative"]), "100.00")
        self.assertEqual(M(a["unallocated"]["above_layer"]), "50.00")
        l1 = prog_layer(jan, "L1")
        self.assertEqual(M(l1["ceded"]["cumulative"]), "100.00")
        self.assertEqual(l1["reinstatements_used"], 0)

        # second claim pushes the layer aggregate past the first limit:
        # one reinstatement is used and premium is charged on the reinstated 100
        p.ingest(batch("B2", "2026-02",
                       risks=[risk("RB")], claims=[claim("B", "RB")],
                       movements=[mv("M2", "B", amount="120")]))
        feb = p.close_all()[0]
        l1 = prog_layer(feb, "L1")
        self.assertEqual(M(l1["ceded"]["cumulative"]), "200.00")
        self.assertEqual(M(l1["aggregate_capacity"]), "200.00")
        self.assertEqual(l1["reinstatements_used"], 1)
        self.assertEqual(M(l1["reinstated_amount"]["cumulative"]), "100.00")
        self.assertEqual(M(l1["reinstatement_premium"]["cumulative"]), "100.00")

        # a recovery in March reduces the cession and the reinstatement premium
        p.ingest(batch("B3", "2026-03",
                       movements=[mv("M3", "A", type="RECOVERY", amount="60")]))
        mar = p.close_all()[0]
        a = claim_row(mar, "A")
        self.assertEqual(M(a["ground_up"]["cumulative"]), "90.00")
        self.assertEqual(M(a["recoveries"]["cumulative"]), "60.00")
        self.assertEqual(M(clayer(a, "L1")["ceded"]["movement"]), "-10.00")
        l1 = prog_layer(mar, "L1")
        self.assertEqual(M(l1["ceded"]["cumulative"]), "190.00")
        self.assertEqual(M(l1["reinstatement_premium"]["movement"]), "-10.00")
        self.assertEqual(M(l1["reinstatement_premium"]["cumulative"]), "90.00")
        self.assertEqual(mar["totals"]["conservation"], "ok")


# ---------------------------------------------------------------------------
# 4. FX conversions are frozen; revisions only affect later confirmations
# ---------------------------------------------------------------------------

class TestFxFreezeAndRevision(unittest.TestCase):
    def test_frozen_rates_and_revision(self):
        p = make_pipeline(
            [program(versions=[version(1, "2026-01", None, [
                layer("L1", "100", "50")])])],
            fx_docs=[fx("2026-01", {"EUR": "1.08"})])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1", ccy="EUR")],
                       claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="100", ccy="EUR")]))
        # the rate table is revised after B1 was confirmed
        rev = p.register_fx(fx("2026-01", {"EUR": "1.20"}, rev=2))
        self.assertEqual(rev["status"], "ok")
        p.ingest(batch("B2", "2026-01",
                       movements=[mv("M2", "C1", amount="100", ccy="EUR")]))
        jan = p.close_all()[0]

        c1 = claim_row(jan, "C1")
        # 100 EUR at the frozen 1.08 + 100 EUR at the revised 1.20
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "228.00")
        moves = {m["movement_id"]: m for m in jan["movements"]}
        self.assertEqual(moves["M1"]["fx"]["rate"], "1.080000")
        self.assertEqual(moves["M1"]["fx"]["revision"], 1)
        self.assertEqual(moves["M2"]["fx"]["rate"], "1.200000")
        self.assertEqual(moves["M2"]["fx"]["revision"], 2)
        self.assertEqual(jan["fx_snapshots"], ["FX-2026-01:1", "FX-2026-01:2"])

        # a rerun never re-rates confirmed events
        self.assertTrue(p.rebuild()["consistent"])
        again = p.ingest(batch("B1", "2026-01", risks=[risk("R1", ccy="EUR")],
                               claims=[claim("C1", "R1")],
                               movements=[mv("M1", "C1", amount="100", ccy="EUR")]))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(M(claim_row(close_of(p, "2026-01"), "C1")["ground_up"]["cumulative"]),
                         "228.00")

        # an already-registered revision is immutable
        tamper = p.register_fx(fx("2026-01", {"EUR": "9.99"}, rev=1))
        self.assertEqual(tamper["status"], "error")


# ---------------------------------------------------------------------------
# 5. idempotency: duplicate files and duplicate movements
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):
    def test_duplicate_batch_and_movement_are_ignored(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        doc = batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                    movements=[mv("M1", "C1", amount="120")])
        first = p.ingest(copy.deepcopy(doc))
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(len(p.store.events), 1)

        second = p.ingest(copy.deepcopy(doc))
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(p.store.events), 1)

        # the same movement inside a different batch is deduped by movement_id
        other = p.ingest(batch("B2", "2026-01",
                               movements=[mv("M1", "C1", amount="120"),
                                          mv("M2", "C1", amount="30")]))
        self.assertEqual(other["status"], "confirmed")
        self.assertEqual(other["duplicates"], 1)
        self.assertEqual(other["confirmed"], 1)
        self.assertEqual(len(p.store.events), 2)

        # the same movement_id with different content is a conflict, not a booking
        conflict = p.ingest(batch("B3", "2026-01",
                                  movements=[mv("M1", "C1", amount="999")]))
        self.assertEqual(conflict["status"], "rejected")
        self.assertIn("different content", conflict["errors"][0]["reason"])
        self.assertEqual(len(p.store.events), 2)

        # the same batch_id with different content is rejected outright
        tampered = p.ingest(batch("B1", "2026-01",
                                  movements=[mv("M9", "C1", amount="1")]))
        self.assertEqual(tampered["status"], "rejected")
        self.assertIn("re-used", tampered["reason"])

        jan = p.close_all()[0]
        self.assertEqual(M(claim_row(jan, "C1")["ground_up"]["cumulative"]), "150.00")
        self.assertTrue(p.rebuild()["consistent"])


# ---------------------------------------------------------------------------
# 6. input permutation does not change results
# ---------------------------------------------------------------------------

class TestInputPermutation(unittest.TestCase):
    # L1 has two reinstatements, so its aggregate capacity never binds here;
    # where a cap does bind, first-reported order is the documented tie-break
    SCENARIO_PROGRAM = [program(versions=[version(1, "2026-01", None, [
        layer("L1", "100", "50", rein=2, rate="1.0"), layer("L2", "150", "1000")])])]

    def _ingest_scenario(self, p, reverse_batches, shuffle_rows):
        # each batch is self-contained: it registers the claims its movements
        # reference, so batch arrival order does not matter
        b1 = batch("B1", "2026-01",
                   risks=[risk("R1"), risk("R2")],
                   claims=[claim("C1", "R1"), claim("C2", "R2")],
                   movements=[mv("M1", "C1", amount="120"),
                              mv("M2", "C2", amount="200"),
                              mv("M3", "C1", type="CORRECTION", amount="180", target="M1")])
        b2 = batch("B2", "2026-02",
                   risks=[risk("R3")], claims=[claim("C3", "R3")],
                   movements=[mv("M4", "C3", amount="300"),
                              mv("M5", "C3", type="RECOVERY", amount="40"),
                              mv("M6", "C3", amount="50")])
        docs = [b1, b2]
        if reverse_batches:
            docs = [b2, b1]
        for doc in docs:
            if shuffle_rows:
                doc["movements"] = list(reversed(doc["movements"]))
                doc["risks"] = list(reversed(doc["risks"]))
                doc["claims"] = list(reversed(doc["claims"]))
            result = p.ingest(doc)
            self.assertEqual(result["status"], "confirmed", result)
        p.close_all()

    def test_permuted_inputs_produce_identical_closes(self):
        p1 = make_pipeline(self.SCENARIO_PROGRAM)
        self._ingest_scenario(p1, reverse_batches=False, shuffle_rows=False)
        p2 = make_pipeline(self.SCENARIO_PROGRAM)
        self._ingest_scenario(p2, reverse_batches=True, shuffle_rows=True)

        def strip_provenance(obj):
            # "row" points into the physical file, so it legitimately differs
            # when rows are permuted; everything economic must be identical
            if isinstance(obj, dict):
                return {k: strip_provenance(v) for k, v in obj.items() if k != "row"}
            if isinstance(obj, list):
                return [strip_provenance(v) for v in obj]
            return obj

        self.assertEqual(dumps(strip_provenance(json_ready(p1.store.closes))),
                         dumps(strip_provenance(json_ready(p2.store.closes))))
        feb = close_of(p1, "2026-02")
        # C1: 120 corrected to 180; C2: 200; C3: 300 - 40 + 50 = 310
        self.assertEqual(M(claim_row(feb, "C1")["ground_up"]["cumulative"]), "180.00")
        self.assertEqual(M(claim_row(feb, "C2")["ground_up"]["cumulative"]), "200.00")
        self.assertEqual(M(claim_row(feb, "C3")["ground_up"]["cumulative"]), "310.00")
        self.assertEqual(M(feb["totals"]["ground_up"]["cumulative"]), "690.00")
        self.assertEqual(M(feb["totals"]["ceded"]["cumulative"]), "390.00")
        self.assertEqual(feb["totals"]["conservation"], "ok")


# ---------------------------------------------------------------------------
# 7. bad rows are quarantined; good rows in the same batch still confirm
# ---------------------------------------------------------------------------

class TestPartialBatch(unittest.TestCase):
    def test_bad_rows_isolated(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        result = p.ingest(batch(
            "B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
            movements=[
                mv("M1", "C1", amount="120"),                       # good
                mv("M2", "C1", amount="-5"),                        # negative amount
                mv("M3", "C1", type="LEVITATE", amount="5"),        # unknown type
                mv("M4", "C1", type="CORRECTION", amount="5", target="NOPE"),  # bad target
                mv("M5", "C1", amount="30"),                        # good
            ]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["confirmed"], 2)
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(len(p.store.events), 2)
        self.assertEqual(len(p.store.quarantine), 3)
        jan = p.close_all()[0]
        self.assertEqual(M(claim_row(jan, "C1")["ground_up"]["cumulative"]), "150.00")
        self.assertEqual(jan["totals"]["conservation"], "ok")

    def test_malformed_file_rejects_only_its_own_batch(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        bad = p.ingest_text("{not json", fmt="json", source="broker-x",
                            batch_id="BX-1", period="2026-01")
        self.assertEqual(bad["status"], "rejected")
        good = p.ingest(batch("B2", "2026-01", risks=[risk("R1")],
                              claims=[claim("C1", "R1")],
                              movements=[mv("M1", "C1", amount="120")]))
        self.assertEqual(good["status"], "confirmed")
        self.assertEqual(len(p.store.rejected_attempts), 1)
        jan = p.close_all()[0]
        self.assertEqual(M(claim_row(jan, "C1")["ground_up"]["cumulative"]), "120.00")

    def test_closed_period_rejects_late_batch(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="120")]))
        p.close_all()
        late = p.ingest(batch("B2", "2026-01", movements=[mv("M2", "C1", amount="10")]))
        self.assertEqual(late["status"], "rejected")
        self.assertIn("already closed", late["reason"])
        self.assertEqual(len(p.store.events), 1)


# ---------------------------------------------------------------------------
# 8. treaty versions: effective ranges, replay, immutability
# ---------------------------------------------------------------------------

class TestTreatyVersions(unittest.TestCase):
    def _pipeline(self):
        return make_pipeline([program(versions=[
            version(1, "2026-01", "2026-02",
                    [layer("L1", "100", "50"), layer("L2", "150", "100")]),
            version(2, "2026-03", None, [layer("L1", "100", "80")]),
        ])])

    def test_versions_govern_only_their_range_and_replay(self):
        p = self._pipeline()
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="200")]))
        p.close_all()
        jan = close_of(p, "2026-01")
        self.assertEqual(jan["treaty_versions"], {"P": 1})
        c1 = claim_row(jan, "C1")
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "50.00")
        self.assertEqual(M(clayer(c1, "L2")["ceded"]["cumulative"]), "50.00")

        p.ingest(batch("B2", "2026-03", movements=[mv("M2", "C1", amount="60")]))
        p.close_all()
        mar = close_of(p, "2026-03")
        self.assertEqual(mar["treaty_versions"], {"P": 2})
        c1 = claim_row(mar, "C1")
        # version 2 has no L2: its March cession is released back to unallocated
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "80.00")
        removed = clayer(c1, "L2")
        self.assertTrue(removed["removed_in_current_version"])
        self.assertEqual(M(removed["ceded"]["movement"]), "-50.00")
        self.assertEqual(M(c1["unallocated"]["total"]), "180.00")
        self.assertEqual(mar["totals"]["conservation"], "ok")

        # replay of January reproduces the stored close exactly ...
        replayed = p.replay("2026-01")
        self.assertEqual(dumps(json_ready(replayed)), dumps(close_of(p, "2026-01")))
        # ... and replaying January under version 2 shows the counterfactual
        # without touching the stored close
        counterfactual = p.replay("2026-01", program="P", version=2)
        self.assertTrue(counterfactual["replayed"])
        self.assertEqual(M(clayer(claim_row(counterfactual, "C1"), "L1")
                           ["ceded"]["cumulative"]), "80.00")
        self.assertEqual(dumps(close_of(p, "2026-01")), dumps(jan))

    def test_version_registration_cannot_rewrite_closed_periods(self):
        p = self._pipeline()
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="200")]))
        p.ingest(batch("B2", "2026-02", movements=[mv("M2", "C1", amount="10")]))
        p.close_all()
        self.assertEqual(sorted(p.store.closes), ["2026-01", "2026-02"])

        # mutating an existing version's layers is rejected
        mutated = program(versions=[
            version(1, "2026-01", "2026-02", [layer("L1", "100", "60"),
                                            layer("L2", "150", "100")]),
            version(2, "2026-03", None, [layer("L1", "100", "80")])])
        self.assertEqual(p.register_program(mutated)["status"], "error")

        # shortening v1 so that closed 2026-02 loses its governing version is
        # rejected: a version change may only affect its own effective range
        remapping = program(versions=[
            version(1, "2026-01", "2026-01",
                    [layer("L1", "100", "50"), layer("L2", "150", "100")]),
            version(2, "2026-03", None, [layer("L1", "100", "80")])])
        rejected = p.register_program(remapping)
        self.assertEqual(rejected["status"], "error")
        self.assertIn("closed period", rejected["reason"])

        # renewal done right: close off v2 at 2026-03 and append v3 — allowed
        # while 2026-03 is still open, because no closed period is remapped
        renewal = program(versions=[
            version(1, "2026-01", "2026-02",
                    [layer("L1", "100", "50"), layer("L2", "150", "100")]),
            version(2, "2026-03", "2026-03", [layer("L1", "100", "80")]),
            version(3, "2026-04", None, [layer("L1", "100", "90")])])
        self.assertEqual(p.register_program(renewal)["status"], "ok")


# ---------------------------------------------------------------------------
# 9. out-of-order rows inside a CSV file
# ---------------------------------------------------------------------------

class TestCsvOutOfOrder(unittest.TestCase):
    def test_correction_before_its_target_in_one_file(self):
        p = make_pipeline([program(pid="PROP-XOL", versions=[
            version(1, "2026-01", "2026-03",
                    [layer("L1", "100", "50", rein=1, rate="1.0"),
                     layer("L2", "150", "1000")]),
            version(2, "2026-04", None,
                    [layer("L1", "100", "50", rein=1, rate="1.0"),
                     layer("L2", "150", "700")])])],
            fx_docs=[fx("2026-03", {"EUR": "1.09", "GBP": "1.25"})])
        text = (FIXTURES / "batches" / "broker_b__BB-2026-03__2026-03.csv").read_text()
        result = p.ingest_text(text, fmt="csv", source="broker_b",
                               batch_id="BB-2026-03", period="2026-03")
        self.assertEqual(result["status"], "confirmed")
        mar = p.close_all()[0]
        c9 = claim_row(mar, "C9")
        # 200 added, corrected to 260, 10 recovered: 250, not 200+260-10
        self.assertEqual(M(c9["ground_up"]["cumulative"]), "250.00")
        self.assertEqual(M(c9["recoveries"]["cumulative"]), "10.00")
        self.assertEqual(M(clayer(c9, "L1")["ceded"]["cumulative"]), "50.00")
        self.assertEqual(M(clayer(c9, "L2")["ceded"]["cumulative"]), "100.00")
        self.assertEqual(M(c9["unallocated"]["retention"]), "100.00")

    def test_csv_missing_columns_is_a_batch_rejection(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "0", "100")])])])
        result = p.ingest_text("a,b,c\n1,2,3\n", fmt="csv", source="s",
                               batch_id="B", period="2026-01")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("missing required columns", result["reason"])


# ---------------------------------------------------------------------------
# 10. unallocated decomposition, gaps, negative positions
# ---------------------------------------------------------------------------

class TestUnallocatedDecomposition(unittest.TestCase):
    def test_gap_between_layers_and_above_top(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50"),    # exhausts at 150
            layer("L2", "200", "100"),   # attaches at 200, exhausts at 300
        ])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="250")]))
        jan = p.close_all()[0]
        u = claim_row(jan, "C1")["unallocated"]
        self.assertEqual(M(u["retention"]), "100.00")
        self.assertEqual(M(u["gap"]), "50.00")
        self.assertEqual(M(u["above_layer"]), "0.00")
        self.assertEqual(M(u["total"]), "150.00")

        p.ingest(batch("B2", "2026-02", movements=[mv("M2", "C1", amount="150")]))
        feb = p.close_all()[0]
        u = claim_row(feb, "C1")["unallocated"]
        self.assertEqual(M(u["retention"]), "100.00")
        self.assertEqual(M(u["gap"]), "50.00")
        self.assertEqual(M(u["above_layer"]), "100.00")
        self.assertEqual(M(u["total"]), "250.00")
        self.assertEqual(feb["totals"]["conservation"], "ok")

    def test_negative_cumulative_is_flagged_not_silent(self):
        p = make_pipeline([program(versions=[version(1, "2026-01", None, [
            layer("L1", "100", "50")])])])
        p.ingest(batch("B1", "2026-01", risks=[risk("R1")], claims=[claim("C1", "R1")],
                       movements=[mv("M1", "C1", amount="100")]))
        p.close_all()
        p.ingest(batch("B2", "2026-02",
                       movements=[mv("M2", "C1", type="RECOVERY", amount="150")]))
        feb = p.close_all()[0]
        c1 = claim_row(feb, "C1")
        self.assertEqual(M(c1["ground_up"]["cumulative"]), "-50.00")
        self.assertTrue(c1["errors"])
        self.assertIn("C1", feb["claims_with_errors"])
        self.assertEqual(M(clayer(c1, "L1")["ceded"]["cumulative"]), "0.00")
        self.assertEqual(feb["totals"]["conservation"], "ok")


# ---------------------------------------------------------------------------
# 11. audit consistency: rebuild and full determinism
# ---------------------------------------------------------------------------

class TestAuditConsistency(unittest.TestCase):
    def test_rebuild_matches_incremental_closes_and_output_is_deterministic(self):
        payload = json.loads((FIXTURES / "input.json").read_text())
        first = run(payload)
        second = run(json.loads((FIXTURES / "input.json").read_text()))
        self.assertEqual(dumps(first), dumps(second))
        self.assertEqual(first["status"], "ok")
        self.assertTrue(first["rebuild"]["consistent"])
        for period in first["periods"]:
            self.assertEqual(period["totals"]["conservation"], "ok")
            self.assertEqual(period["totals"]["conservation_delta"], "0.00")

    def test_demo_fixture_story(self):
        report = run(json.loads((FIXTURES / "input.json").read_text()))
        periods = {p["period"]: p for p in report["periods"]}

        jan = periods["2026-01"]
        self.assertEqual(jan["treaty_versions"], {"PROP-XOL": 1})
        self.assertEqual(jan["totals"]["ground_up"]["cumulative"], "444.00")
        self.assertEqual(jan["totals"]["ceded"]["cumulative"], "244.00")
        self.assertEqual(jan["totals"]["unallocated_cumulative"], "200.00")

        feb = periods["2026-02"]
        # correction (M1: 120 -> 400), recovery (24 EUR), new GBP claim,
        # FX revision 1.10 -> 1.12 between the two February batches
        self.assertEqual(feb["fx_snapshots"], ["FX-2026-02:1", "FX-2026-02:2"])
        c1 = next(c for c in feb["claims"] if c["claim_id"] == "C1")
        l1 = next(l for l in c1["layers"] if l["layer_id"] == "L1")
        self.assertEqual(l1["ceded"], {"movement": "30.00", "cumulative": "50.00"})
        c2 = next(c for c in feb["claims"] if c["claim_id"] == "C2")
        self.assertEqual(c2["recoveries"]["cumulative"], "26.40")
        self.assertEqual(c2["ground_up"]["cumulative"], "353.60")
        c3 = next(c for c in feb["claims"] if c["claim_id"] == "C3")
        l1_c3 = next(l for l in c3["layers"] if l["layer_id"] == "L1")
        # L1 aggregate (limit 50, one reinstatement) is exhausted by C1 and C2,
        # so C3's 50 falls into above-aggregate unallocated
        self.assertEqual(l1_c3["ceded"]["cumulative"], "0.00")
        self.assertEqual(l1_c3["aggregate_shortfall"], "50.00")
        prog_l1 = next(l for l in feb["programs"][0]["layers"] if l["layer_id"] == "L1")
        self.assertEqual(prog_l1["reinstatements_used"], 1)
        self.assertEqual(prog_l1["reinstatement_premium"]["cumulative"], "50.00")
        self.assertEqual(feb["totals"]["ceded"]["cumulative"], "1033.60")

        apr = periods["2026-04"]
        # treaty version 2 (L2 limit 1000 -> 700) applies from April only:
        # C3's L2 cession is clawed back with a negative movement
        self.assertEqual(apr["treaty_versions"], {"PROP-XOL": 2})
        c3 = next(c for c in apr["claims"] if c["claim_id"] == "C3")
        l2_c3 = next(l for l in c3["layers"] if l["layer_id"] == "L2")
        self.assertEqual(l2_c3["ceded"]["movement"], "-308.60")
        self.assertEqual(l2_c3["ceded"]["cumulative"], "171.40")
        self.assertEqual(apr["totals"]["ceded"]["movement"], "-258.60")
        self.assertEqual(apr["totals"]["ceded"]["cumulative"], "800.00")

        # March replay under version 1 still matches the stored close
        pipeline = Pipeline()
        parser_split = parser.split_bundle(
            json.loads((FIXTURES / "input.json").read_text()))
        from reinsurance_bordereaux_normalizer.pipeline import apply_ops
        apply_ops(pipeline, parser_split)
        pipeline.close_all()
        replayed = pipeline.replay("2026-03")
        self.assertEqual(dumps(json_ready(replayed)),
                         dumps(pipeline.store.closes["2026-03"]))

        # batch outcomes: partial batch isolated, duplicate movement ignored
        batches = {b["batch_id"]: b for b in report["batches"]}
        self.assertEqual(batches["BA-2026-02"]["status"], "partial")
        self.assertEqual(batches["BA-2026-02"]["confirmed"], 3)
        self.assertEqual(batches["BA-2026-02B"]["duplicates"], 1)
        self.assertEqual(len(report["quarantine"]), 2)
        self.assertEqual({q["identifier"] for q in report["quarantine"]}, {"M6", "M7"})


if __name__ == "__main__":
    unittest.main()
