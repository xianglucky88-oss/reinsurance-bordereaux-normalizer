def calculate(rows, payload):
    treaties = sorted(payload.get("treaties", []), key=lambda t: t["attachment"]); cumulative = 0; allocations = []
    for loss in rows:
        start = cumulative; cumulative += float(loss["amount"])
        for treaty in treaties:
            covered_before = max(0.0, min(start, treaty["attachment"] + treaty["limit"]) - treaty["attachment"])
            covered_after = max(0.0, min(cumulative, treaty["attachment"] + treaty["limit"]) - treaty["attachment"])
            amount = round(covered_after - covered_before, 2)
            if amount: allocations.append({"claim": loss["claim"], "movement": loss["movement"], "treaty": treaty["name"], "allocated": amount})
    return {"cumulative_loss": cumulative, "allocations": allocations, "unallocated": max(0.0, cumulative - sum(a["allocated"] for a in allocations))}
