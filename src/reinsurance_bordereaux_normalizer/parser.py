def load_records(payload: dict) -> list[dict]:
    rows = payload.get("losses", [])
    if not isinstance(rows, list):
        raise ValueError("losses must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]
