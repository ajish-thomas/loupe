"""Lazy local-file ingest, streamed CSV caching, and bounded REPL previews."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import polars as pl

CSV_CACHE_THRESHOLD = 64 * 1024 * 1024
DEFAULT_PREVIEW_ROWS = 20
MAX_PREVIEW_ROWS = 100
DEFAULT_INFER_SCHEMA_LENGTH = 10_000

_SCANNERS = {
    ".parquet": pl.scan_parquet,
    ".ndjson": pl.scan_ndjson,
    ".jsonl": pl.scan_ndjson,
    ".arrow": pl.scan_ipc,
    ".ipc": pl.scan_ipc,
}
_ScanObserver = Callable[[Path, pl.Schema], None]
_observer: ContextVar[_ScanObserver | None] = ContextVar("scan_observer", default=None)


@contextlib.contextmanager
def report_scans(observer: _ScanObserver) -> Iterator[None]:
    """Route schema feedback to the current REPL request only."""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@lru_cache(maxsize=1)
def _temporary_cache() -> tempfile.TemporaryDirectory:
    # Keep the owner alive until interpreter exit. The Go host supplies its own
    # directory so it can also clean up after SIGKILL or a native crash.
    return tempfile.TemporaryDirectory(prefix="loupe-data-")


def _cache_directory() -> Path:
    configured = os.environ.get("LOUPE_SESSION_CACHE")
    return Path(configured) if configured else Path(_temporary_cache().name)


def _source_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def scan(
    path: str | Path,
    *,
    schema_overrides: Mapping[str, pl.DataType | type[pl.DataType]] | None = None,
    infer_schema_length: int = DEFAULT_INFER_SCHEMA_LENGTH,
    try_parse_dates: bool = True,
    cache_threshold: int = CSV_CACHE_THRESHOLD,
    cache_dir: str | Path | None = None,
) -> pl.LazyFrame:
    """Scan one local CSV/Parquet/NDJSON/IPC file and return a LazyFrame.

    CSVs at least ``cache_threshold`` bytes are streamed once to a session
    Parquet cache. No source-sized DataFrame is collected. Cache identity includes
    file metadata and CSV parsing options; changing either creates a new entry.
    CSV schema overrides allow correcting inference on re-ingest. Interrupted or
    failed writes are never published as complete cache files.
    """
    path = Path(path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix != ".csv" and suffix not in _SCANNERS:
        supported = ", ".join(sorted([".csv", *_SCANNERS]))
        raise ValueError(f"unsupported file extension {suffix!r} for {path}; supported extensions: {supported}")
    if not path.is_file():
        raise FileNotFoundError(f"not a local data file: {path}")
    if cache_threshold < 0:
        raise ValueError("cache_threshold must be non-negative")
    if not isinstance(infer_schema_length, int) or infer_schema_length < 0:
        raise ValueError("infer_schema_length must be a non-negative integer")

    if suffix == ".csv":
        options = dict(
            schema_overrides=dict(schema_overrides) if schema_overrides else None,
            infer_schema_length=infer_schema_length,
            try_parse_dates=try_parse_dates,
            glob=False,
            low_memory=True,
        )
        lf = pl.scan_csv(path, **options)
        identity = _source_identity(path)
        if identity[2] >= cache_threshold:
            directory = Path(cache_dir) if cache_dir is not None else _cache_directory()
            directory.mkdir(parents=True, exist_ok=True)
            key = json.dumps([str(path), identity, pl.__version__, {
                "infer_schema_length": infer_schema_length,
                "try_parse_dates": try_parse_dates,
                "schema_overrides": sorted((k, str(v)) for k, v in (schema_overrides or {}).items()),
            }], sort_keys=True).encode()
            cached = directory / (hashlib.sha256(key).hexdigest() + ".parquet")
            if not cached.exists():
                fd, name = tempfile.mkstemp(prefix=".csv-", suffix=".parquet", dir=directory)
                os.close(fd)
                temporary = Path(name)
                try:
                    lf.sink_parquet(temporary, row_group_size=65_536, engine="streaming")
                    if _source_identity(path) != identity:
                        raise OSError(f"source changed during ingest; retry scan({str(path)!r})")
                    temporary.replace(cached)
                finally:
                    temporary.unlink(missing_ok=True)
            lf = pl.scan_parquet(cached)
    else:
        if schema_overrides is not None:
            raise ValueError("schema_overrides is supported for CSV files only")
        # Glob expansion is deliberately disabled for file names containing [].
        if suffix in (".parquet", ".ipc", ".arrow"):
            lf = _SCANNERS[suffix](path, glob=False)
        else:
            lf = _SCANNERS[suffix](path)

    observer = _observer.get()
    if observer is not None:
        observer(path, lf.collect_schema())
    return lf


@dataclass(frozen=True)
class Preview:
    """A bounded display result; ``data`` never contains more than ``limit`` rows."""

    data: pl.DataFrame
    limit: int
    has_more: bool


def preview(data: pl.LazyFrame | pl.DataFrame, n: int = DEFAULT_PREVIEW_ROWS) -> Preview:
    """Collect at most n+1 rows, retaining at most n (1–100) for display.

    The extra row indicates truncation without counting or collecting the full
    source. An arbitrary user query may still need a full scan to produce a head
    (for example a global sort); this bounds the result, not every query's work.
    """
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_PREVIEW_ROWS:
        raise ValueError(f"preview rows must be an integer between 1 and {MAX_PREVIEW_ROWS}")
    if isinstance(data, pl.LazyFrame):
        head = data.head(n + 1).collect(engine="streaming")
    elif isinstance(data, pl.DataFrame):
        head = data.head(n + 1)
    else:
        raise TypeError("preview requires a Polars LazyFrame or DataFrame")
    return Preview(head.head(n), n, head.height > n)
