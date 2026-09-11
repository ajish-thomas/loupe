# loupe-kernel

The embedded Python side of `loupe` (see the repo root `README.md`). Runs inside
the extracted CPython tree as `python3 -m loupe_kernel <socket_path>`, speaking a
newline-delimited JSON protocol over a Unix domain socket that the Go host owns.

- `__init__.py` — the persistent REPL loop, stdout/stderr capture, socket protocol
- `ingest.py` — lazy scans, streamed CSV→Parquet cache, bounded previews
- `api.py` — the curated `plot` / `scatter` / `histogram` / `heatmap` commands;
  heatmap preserves x/y/color triples and fixes the color scale before sampling
- `engine.py` — M4 viewport aggregation and batch-and-merge streaming

Managed with [uv](https://docs.astral.sh/uv/): `uv sync`, then `uv run pytest`.
