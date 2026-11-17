"""Deterministic JSON CLI.

Commands
--------
run       Process a bundle file or a fixture directory end-to-end (default).
process   Ingest files into a persistent store and close open periods.
close     Close any open periods in the store.
rebuild   Replay the event log and verify every stored close.
replay    Recompute one closed period, optionally under a pinned treaty version.
report    Print the stored report (or a single period close).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import parser as _parser
from .pipeline import Pipeline, apply_ops
from .report import dumps, json_ready
from .store import Store

DEFAULT_FIXTURE = Path(__file__).parents[2] / "fixtures" / "input.json"
DEFAULT_STORE = ".rbn_store"


def _csv_meta(path: Path):
    parts = path.stem.split("__")
    if len(parts) != 3:
        raise SystemExit(
            f"csv file {path} must be named '<source>__<batch_id>__<period>.csv'")
    return {"source": parts[0], "batch_id": parts[1], "period": parts[2]}


def _ingest_file(pipeline: Pipeline, path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        return [pipeline.ingest_text(text, fmt="csv", **_csv_meta(path))]
    doc = _parser.parse_json_text(text)
    results = []
    for kind, op in _parser.split_bundle(doc):
        if kind == "reference":
            results.append(pipeline.register_reference(op))
        else:
            results.append(pipeline.ingest(op))
    return results


def _load_fixture_dir(pipeline: Pipeline, directory: Path) -> None:
    reference_files = []
    batch_files = []
    ref_dir = directory / "reference"
    batch_dir = directory / "batches"
    if ref_dir.is_dir():
        reference_files.extend(sorted(ref_dir.glob("*.json")))
    single = directory / "reference.json"
    if single.is_file():
        reference_files.append(single)
    if batch_dir.is_dir():
        batch_files.extend(sorted(batch_dir.iterdir()))
    for path in reference_files + batch_files:
        _ingest_file(pipeline, path)


def cmd_run(args) -> int:
    pipeline = Pipeline()
    source = Path(args.input)
    if source.is_dir():
        _load_fixture_dir(pipeline, source)
    else:
        doc = _parser.parse_json_text(source.read_text(encoding="utf-8"))
        apply_ops(pipeline, _parser.split_bundle(doc))
    pipeline.close_all()
    report = pipeline.report()
    report["rebuild"] = pipeline.rebuild()
    print(dumps(report))
    return 0


def cmd_process(args) -> int:
    store = Store.load(args.store)
    pipeline = Pipeline(store)
    ingested = []
    for name in args.files:
        ingested.append({"file": name, "results": _ingest_file(pipeline, Path(name))})
    closes = pipeline.close_all()
    store.save(args.store)
    print(dumps({"ingested": ingested,
                 "closed_periods": [c["period"] for c in closes],
                 "report": pipeline.report()}))
    return 0


def cmd_close(args) -> int:
    store = Store.load(args.store)
    pipeline = Pipeline(store)
    closes = pipeline.close_all()
    store.save(args.store)
    print(dumps({"closed": [c["period"] for c in closes]}))
    return 0


def cmd_rebuild(args) -> int:
    store = Store.load(args.store)
    pipeline = Pipeline(store)
    print(dumps(pipeline.rebuild()))
    return 0


def cmd_replay(args) -> int:
    store = Store.load(args.store)
    pipeline = Pipeline(store)
    close = pipeline.replay(args.period, program=args.program,
                            version=args.treaty_version)
    print(dumps(close))
    return 0


def cmd_report(args) -> int:
    store = Store.load(args.store)
    pipeline = Pipeline(store)
    if args.period:
        close = store.closes.get(args.period)
        if close is None:
            print(dumps({"status": "error",
                         "reason": f"period {args.period} is not closed"}))
            return 1
        print(dumps(close))
        return 0
    print(dumps(pipeline.report()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="reinsurance-bordereaux-normalizer",
        description="Normalize loss movements and allocate them across treaty layers.")
    sub = ap.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="process a bundle file or fixture directory")
    p_run.add_argument("input", nargs="?", default=str(DEFAULT_FIXTURE))
    p_run.set_defaults(func=cmd_run)

    p_proc = sub.add_parser("process", help="ingest files into a store and close periods")
    p_proc.add_argument("--store", default=DEFAULT_STORE)
    p_proc.add_argument("files", nargs="+")
    p_proc.set_defaults(func=cmd_process)

    p_close = sub.add_parser("close", help="close open periods in the store")
    p_close.add_argument("--store", default=DEFAULT_STORE)
    p_close.set_defaults(func=cmd_close)

    p_rebuild = sub.add_parser("rebuild", help="replay the event log and verify closes")
    p_rebuild.add_argument("--store", default=DEFAULT_STORE)
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_replay = sub.add_parser("replay", help="recompute a closed period")
    p_replay.add_argument("--store", default=DEFAULT_STORE)
    p_replay.add_argument("--period", required=True)
    p_replay.add_argument("--program")
    p_replay.add_argument("--treaty-version", type=int)
    p_replay.set_defaults(func=cmd_replay)

    p_report = sub.add_parser("report", help="print the stored report")
    p_report.add_argument("--store", default=DEFAULT_STORE)
    p_report.add_argument("--period")
    p_report.set_defaults(func=cmd_report)
    return ap


def main(argv=None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        args = ap.parse_args(["run", str(DEFAULT_FIXTURE)])
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
