"""M4 viewport aggregation.

Per PLAN.md, "Performance strategy" and "Data engine": a chart cannot show
more than roughly `width` distinct x-positions, so M4 (VLDB 2014,
http://www.vldb.org/pvldb/vol7/p797-jugel.pdf) reduces a series to up to
4 points per pixel-width bucket -- the first, last, min-y, and max-y point
of each bucket -- which traces a line chart's visual envelope with no
perceptible loss at that resolution.

`m4()` is the primary path: the reduction is expressed as Polars lazy
expressions, so Polars applies projection/predicate pushdown and a 1e8-row
Parquet scan is never materialized in Python -- only the aggregated result
(at most `4 * n_buckets` rows, PLAN.md's "~2-8k aggregated points") is ever
collected. `m4_numpy()` is the fallback named in PLAN.md's milestone 6 for
data that already arrived as a bare in-memory numpy array rather than a
LazyFrame.

Both assume numeric (integer or float) x and y columns; Datetime/categorical
x is not yet supported.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True)
class Aggregated:
    """Aggregated (x, y) points ready for the wire, as float32 arrays."""

    x: np.ndarray
    y: np.ndarray

    def __post_init__(self) -> None:
        if self.x.shape != self.y.shape:
            raise ValueError(
                f"x and y must have the same shape, got {self.x.shape} and {self.y.shape}"
            )

    def __len__(self) -> int:
        return self.x.shape[0]


_EMPTY = Aggregated(np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32))

# A full-range M4 over a very large file collects the *entire* filtered
# frame in one shot -- for 1e8 rows that is 6-7 GB RSS, not the "well under
# 800 MB/column" PLAN.md milestone 6 targets. Above `_STREAM_BATCH_ROWS`
# matched rows, reduce fixed-size batches independently (bounded memory)
# and merge the partial per-bucket results -- the merge is the same
# reduction shape applied again, since M4's first/last/min/max per bucket
# are associative. 5_000_000 was empirically tuned on a real 1e8-row
# Parquet file: it is the batch size that is both faster (1.3s vs 1.5s)
# and far less memory-hungry (~1GB vs 6-7GB) than the single-collect path.
_STREAM_BATCH_ROWS = 5_000_000

# Manual batching slices by raw row offset, which bypasses Polars' own
# Parquet row-group pushdown -- fine when the query touches most of the
# file anyway (a wide/full-range zoom), wasteful when it does not (a
# narrow zoom, where pushdown already skips almost all the row groups).
# Route on the fraction of rows the filters actually match: below this,
# the existing single-collect path (with automatic pushdown) wins.
_STREAM_SELECTIVITY_THRESHOLD = 0.2


def m4(
    lf: pl.LazyFrame,
    x: str,
    y: str,
    n_buckets: int,
    x_range: tuple[float, float] | None = None,
) -> Aggregated:
    """Reduce `lf` to up to `4 * n_buckets` points via M4.

    Args:
        lf: the source lazy frame. Only `x` and `y` are read -- Polars
            applies projection pushdown to the rest.
        x, y: numeric column names.
        n_buckets: number of pixel-width buckets, i.e. the chart's pixel
            width. Output is at most `4 * n_buckets` points.
        x_range: the current viewport `(x_min, x_max)`, for zoom-settle
            re-aggregation. Defaults to the full data range, found via a
            cheap min/max collect (PLAN.md milestone 6's "filter() applied
            before plot() measurably reduces scan time" applies here too:
            passing `x_range` lets the filter push into the file scan).

    Returns:
        An `Aggregated` of at most `4 * n_buckets` points, ordered by x.
        Empty if no finite, non-null rows fall in range.
    """
    if n_buckets <= 0:
        raise ValueError(f"n_buckets must be positive, got {n_buckets}")

    # Metadata-only (Parquet footer / row count), not a real scan -- ~1ms
    # regardless of file size (verified empirically) -- so always available
    # for the selectivity check below at negligible cost.
    total_rows = lf.select(pl.len()).collect().item()

    base = lf.select(x, y).filter(
        pl.col(x).is_not_null()
        & pl.col(y).is_not_null()
        & pl.col(x).is_finite()
        & pl.col(y).is_finite()
    )

    if x_range is None:
        if total_rows > _STREAM_BATCH_ROWS:
            # A plain `.filter().select(min, max)` over the whole column
            # pair does not stream the way one might expect -- measured at
            # ~3 GB RSS on a 1e8-row file, the same magnitude as the
            # aggregation problem this function exists to fix. Batch it
            # the same way.
            found = _bounds_streaming(lf, x, y, total_rows)
            if found is None:
                return _EMPTY
            x0, x1, matched_rows = found
        else:
            bounds = base.select(
                pl.col(x).min().alias("x0"),
                pl.col(x).max().alias("x1"),
                pl.len().alias("n"),
            ).collect()
            if bounds.height == 0 or bounds["x0"][0] is None:
                return _EMPTY
            x0, x1 = float(bounds["x0"][0]), float(bounds["x1"][0])
            matched_rows = int(bounds["n"][0])
    else:
        x0, x1 = x_range
        base = base.filter(pl.col(x).is_between(x0, x1))
        # Cheap due to Parquet row-group statistics (verified empirically)
        # -- but still a real collect, so only pay for it once per call.
        matched_rows = base.select(pl.len()).collect().item()
        if matched_rows == 0:
            return _EMPTY

    span = x1 - x0
    if span <= 0:
        # A single distinct x value, or a degenerate/empty range: everything
        # that survives the filters above belongs in one bucket.
        bucket = pl.lit(0, dtype=pl.Int64)
    else:
        bucket = ((pl.col(x) - x0) * n_buckets / span).floor().cast(pl.Int64).clip(0, n_buckets - 1)

    use_streaming = matched_rows > _STREAM_BATCH_ROWS and (
        total_rows == 0 or matched_rows / total_rows >= _STREAM_SELECTIVITY_THRESHOLD
    )

    if use_streaming:
        agg = _m4_streaming(lf, x, y, x0, x1, x_range is not None, bucket, total_rows)
    else:
        agg = (
            base.with_columns(bucket.alias("_bucket"))
            .group_by("_bucket")
            .agg(*_bucket_agg_exprs(x, y))
            .sort("_bucket")
            .collect()
        )

    if agg.height == 0:
        return _EMPTY

    return _flatten_buckets(agg)


def _bucket_agg_exprs(x: str, y: str) -> list[pl.Expr]:
    """The M4 per-bucket reduction: first/last/min-y/max-y.

    "First" and "last" are reformulated as "the point at x's min" and "the
    point at x's max" (`arg_min`/`arg_max` + `gather`, not a global `.sort()`
    + positional `.first()`/`.last()`). This is mathematically equivalent
    (verified empirically against the previous sort-based formulation) and,
    unlike a sort, is associative -- so the exact same expressions apply to
    an unsorted batch in `_m4_streaming` below, and its partial results can
    be re-merged with `_merge_agg_exprs()`.
    """
    return [
        pl.col(x).gather(pl.col(x).arg_min()).first().alias("x_first"),
        pl.col(y).gather(pl.col(x).arg_min()).first().alias("y_first"),
        pl.col(x).gather(pl.col(x).arg_max()).first().alias("x_last"),
        pl.col(y).gather(pl.col(x).arg_max()).first().alias("y_last"),
        pl.col(x).gather(pl.col(y).arg_min()).first().alias("x_min"),
        pl.col(y).min().alias("y_min"),
        pl.col(x).gather(pl.col(y).arg_max()).first().alias("x_max"),
        pl.col(y).max().alias("y_max"),
    ]


def _merge_agg_exprs() -> list[pl.Expr]:
    """Merge per-batch `_bucket_agg_exprs()` results for the same bucket.

    Same reduction shape as `_bucket_agg_exprs()`, applied one level up: the
    global first-by-x among a bucket's per-batch firsts is that bucket's
    true first, and so on for last/min/max.
    """
    return [
        pl.col("x_first").gather(pl.col("x_first").arg_min()).first(),
        pl.col("y_first").gather(pl.col("x_first").arg_min()).first(),
        pl.col("x_last").gather(pl.col("x_last").arg_max()).first(),
        pl.col("y_last").gather(pl.col("x_last").arg_max()).first(),
        pl.col("x_min").gather(pl.col("y_min").arg_min()).first(),
        pl.col("y_min").min(),
        pl.col("x_max").gather(pl.col("y_max").arg_max()).first(),
        pl.col("y_max").max(),
    ]


def _bounds_streaming(
    lf: pl.LazyFrame, x: str, y: str, total_rows: int
) -> tuple[float, float, int] | None:
    """Batched (x_min, x_max, matched_row_count) for the full data range.

    Same batch-by-raw-offset shape as `_m4_streaming`, for the same reason:
    a plain `filter().select(min, max)` over the whole file measured at
    ~3 GB RSS on a 1e8-row file (Polars does not stream that combination
    the way one might expect), while this batched form measured ~450 MB.

    Returns `None` if no finite, non-null row exists anywhere.
    """
    filter_expr = (
        pl.col(x).is_not_null() & pl.col(y).is_not_null() & pl.col(x).is_finite() & pl.col(y).is_finite()
    )

    x0: float | None = None
    x1: float | None = None
    matched_rows = 0
    offset = 0
    while offset < total_rows:
        batch = (
            lf.slice(offset, _STREAM_BATCH_ROWS)
            .select(x, y)
            .filter(filter_expr)
            .select(pl.col(x).min().alias("x0"), pl.col(x).max().alias("x1"), pl.len().alias("n"))
            .collect()
        )
        offset += _STREAM_BATCH_ROWS
        n = int(batch["n"][0])
        if n == 0:
            continue
        matched_rows += n
        bx0, bx1 = float(batch["x0"][0]), float(batch["x1"][0])
        x0 = bx0 if x0 is None else min(x0, bx0)
        x1 = bx1 if x1 is None else max(x1, bx1)

    if matched_rows == 0:
        return None
    return x0, x1, matched_rows


def _m4_streaming(
    lf: pl.LazyFrame,
    x: str,
    y: str,
    x0: float,
    x1: float,
    has_range: bool,
    bucket: pl.Expr,
    total_rows: int,
) -> pl.DataFrame:
    """Batch-and-merge M4 over `lf`, bounded to `_STREAM_BATCH_ROWS`/batch.

    Slices `lf` by raw row offset -- covering the whole file, not just the
    matched rows -- since `.slice()` on a Parquet scan is push-down-cheap
    regardless of offset (verified empirically), and filtering is applied
    per batch after slicing.
    """
    filter_expr = (
        pl.col(x).is_not_null() & pl.col(y).is_not_null() & pl.col(x).is_finite() & pl.col(y).is_finite()
    )
    if has_range:
        filter_expr = filter_expr & pl.col(x).is_between(x0, x1)

    partials: list[pl.DataFrame] = []
    offset = 0
    while offset < total_rows:
        batch = (
            lf.slice(offset, _STREAM_BATCH_ROWS)
            .select(x, y)
            .filter(filter_expr)
            .with_columns(bucket.alias("_bucket"))
            .group_by("_bucket")
            .agg(*_bucket_agg_exprs(x, y))
            .collect()
        )
        if batch.height > 0:
            partials.append(batch)
        offset += _STREAM_BATCH_ROWS

    if not partials:
        return pl.DataFrame()

    return (
        pl.concat(partials)
        .lazy()
        .group_by("_bucket")
        .agg(*_merge_agg_exprs())
        .sort("_bucket")
        .collect()
    )


def _flatten_buckets(agg: pl.DataFrame) -> Aggregated:
    """Turn one row per bucket into flat, x-sorted (x, y) arrays.

    Each bucket contributes at most 4 points (first, min-y, max-y, last);
    fewer once coincident points collapse, e.g. a single-row bucket where
    first == min == max == last. `agg` has at most `n_buckets` rows, so a
    Python-level loop here is cheap regardless of the source's size.
    """
    x_first, y_first = to_f32(agg["x_first"]), to_f32(agg["y_first"])
    x_min, y_min = to_f32(agg["x_min"]), to_f32(agg["y_min"])
    x_max, y_max = to_f32(agg["x_max"]), to_f32(agg["y_max"])
    x_last, y_last = to_f32(agg["x_last"]), to_f32(agg["y_last"])

    out_x: list[float] = []
    out_y: list[float] = []
    for i in range(len(x_first)):
        points = sorted(
            {
                (x_first[i], y_first[i]),
                (x_min[i], y_min[i]),
                (x_max[i], y_max[i]),
                (x_last[i], y_last[i]),
            }
        )
        out_x.extend(p[0] for p in points)
        out_y.extend(p[1] for p in points)

    return Aggregated(np.asarray(out_x, dtype=np.float32), np.asarray(out_y, dtype=np.float32))


def to_f32(series: pl.Series) -> np.ndarray:
    """Convert a Polars Series to a float32 numpy array without a silent copy.

    Per PLAN.md, "Zero-copy is conditional -- enforce it": `to_numpy()`
    silently falls back to a full copy whenever a series has nulls, more
    than one chunk, or is requested writable -- invisible unless it is
    forbidden outright. `allow_copy=False` makes that fallback a loud
    failure instead of a silent 800 MB-per-column copy, which matters if a
    bug ever lets a full-size (rather than already-aggregated) column reach
    this function. Casting to Float32 makes one legitimate copy -- there is
    no in-place way to narrow float64 to float32 -- so `to_numpy` on the
    cast result must then be free.
    """
    return series.rechunk().cast(pl.Float32).to_numpy(allow_copy=False)


def m4_numpy(
    x: np.ndarray,
    y: np.ndarray,
    n_buckets: int,
    x_range: tuple[float, float] | None = None,
) -> Aggregated:
    """M4 for a bare in-memory numpy array.

    This is the fallback named in PLAN.md's milestone 6 for data that did
    not arrive via a LazyFrame. Requires `x` sorted ascending: unlike `m4()`,
    which sorts as part of its lazy plan, this fallback is for data already
    held in memory, where an extra O(n log n) sort would defeat the point.
    """
    if n_buckets <= 0:
        raise ValueError(f"n_buckets must be positive, got {n_buckets}")
    if x.ndim != 1 or y.ndim != 1:
        raise ValueError("x and y must be 1-D arrays")
    if x.shape != y.shape:
        raise ValueError(f"x and y must have the same shape, got {x.shape} and {y.shape}")

    finite = np.isfinite(x) & np.isfinite(y)
    x, y = x[finite], y[finite]

    if x.size == 0:
        return _EMPTY

    if np.any(np.diff(x) < 0):
        raise ValueError("m4_numpy requires x sorted ascending")

    if x_range is None:
        x0, x1 = float(x[0]), float(x[-1])
    else:
        x0, x1 = x_range
        mask = (x >= x0) & (x <= x1)
        x, y = x[mask], y[mask]
        if x.size == 0:
            return _EMPTY

    span = x1 - x0
    if span <= 0:
        bucket = np.zeros(x.size, dtype=np.int64)
    else:
        bucket = np.floor((x - x0) * n_buckets / span).astype(np.int64)
        np.clip(bucket, 0, n_buckets - 1, out=bucket)

    # x (and therefore bucket) is sorted ascending, so bucket boundaries
    # are exactly where consecutive buckets differ: an O(n_buckets) walk
    # over precomputed boundaries, not an O(n_buckets) rescan of x.
    edges = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [x.size]))

    out_x: list[float] = []
    out_y: list[float] = []
    for start, end in zip(starts, ends):
        bx, by = x[start:end], y[start:end]
        i_min, i_max = int(np.argmin(by)), int(np.argmax(by))
        points = sorted(
            {(bx[0], by[0]), (bx[i_min], by[i_min]), (bx[i_max], by[i_max]), (bx[-1], by[-1])}
        )
        out_x.extend(p[0] for p in points)
        out_y.extend(p[1] for p in points)

    return Aggregated(
        np.asarray(out_x, dtype=np.float32),
        np.asarray(out_y, dtype=np.float32),
    )
