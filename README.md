# Reinsurance Bordereaux Normalizer

Normalize loss movements and allocate them across treaty layers.

## Modules

- `src/reinsurance_bordereaux_normalizer/models.py`: immutable result contract.
- `src/reinsurance_bordereaux_normalizer/parser.py`: input validation and record extraction.
- `src/reinsurance_bordereaux_normalizer/engine.py`: domain calculation only.
- `src/reinsurance_bordereaux_normalizer/report.py`: stable output envelope.
- `src/reinsurance_bordereaux_normalizer/pipeline.py`: orchestration boundary.
- `src/reinsurance_bordereaux_normalizer/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m reinsurance_bordereaux_normalizer.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.
