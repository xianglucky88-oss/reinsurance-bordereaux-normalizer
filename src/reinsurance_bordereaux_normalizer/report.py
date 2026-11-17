"""Stable output envelope: JSON-ready conversion and deterministic dumps.

Invariant: by the time data reaches the report, every ``Decimal`` is a money
amount (rates are rendered to strings at the model boundary), so conversion
here is unambiguous.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .models import PipelineResult, money_str


def json_ready(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return money_str(obj)
    if isinstance(obj, dict):
        return {k: json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_ready(v) for v in obj]
    return obj


def build_result(project: str, data: dict) -> dict:
    result = PipelineResult(project=project, data=data)
    return {"project": result.project, "status": "ok", **result.data}


def dumps(obj: Any) -> str:
    return json.dumps(json_ready(obj), ensure_ascii=False, indent=2, sort_keys=True)
