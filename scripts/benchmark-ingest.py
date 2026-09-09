"""Opt-in milestone 4 check: python/.venv/bin/python scripts/benchmark-ingest.py.

Writes a 100M-row CSV in a temporary directory, then measures ingest and preview
in a fresh process so fixture generation does not inflate the reported peak RSS.
No source-sized Python/Polars DataFrame is constructed. Files are removed on exit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time


def measure(path: Path, cache: Path) -> None:
    import polars as pl
    from loupe_kernel.ingest import preview, scan

    started = time.perf_counter()
    lf = scan(path, cache_dir=cache, cache_threshold=0)
    ingest_seconds = time.perf_counter() - started
    schema = {name: str(dtype) for name, dtype in lf.collect_schema().items()}
    started = time.perf_counter()
    result = preview(lf)
    preview_seconds = time.perf_counter() - started
    started = time.perf_counter()
    scan(path, cache_dir=cache, cache_threshold=0)
    reuse_seconds = time.perf_counter() - started
    print(json.dumps({
        "csv_bytes": path.stat().st_size,
        "rows": lf.select(pl.len()).collect().item(),
        "ingest_seconds": ingest_seconds,
        "preview_seconds": preview_seconds,
        "cache_reuse_seconds": reuse_seconds,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "schema": schema,
        "preview_rows": result.data.height,
        "has_more": result.has_more,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000_000)
    parser.add_argument("--measure", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    if args.measure is not None:
        measure(args.measure, args.cache)
        return
    if args.rows <= 0:
        parser.error("--rows must be positive")
    with tempfile.TemporaryDirectory(prefix="loupe-ingest-benchmark-") as directory:
        root = Path(directory)
        path = root / "data.csv"
        chunk_rows = min(args.rows, 100_000)
        chunk = "".join(f"{i},{i % 97}\n" for i in range(chunk_rows)).encode()
        with path.open("wb") as target:
            target.write(b"x,y\n")
            for _ in range(args.rows // chunk_rows):
                target.write(chunk)
            for i in range(args.rows % chunk_rows):
                target.write(f"{i},{i % 97}\n".encode())
        subprocess.run([
            sys.executable, __file__, "--measure", str(path), "--cache", str(root / "cache")
        ], check=True)


if __name__ == "__main__":
    main()
