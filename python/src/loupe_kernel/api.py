"""Tier-1 curated plotting commands.

Per PLAN.md, "Rendering is tiered": these are the built-in `plot`/
`scatter`/`hist`-style commands, as opposed to tier 2 (arbitrary matplotlib
code). Each stays lazy end to end when given a LazyFrame -- the kind
`ingest.scan()` produces -- so a 1e8-row source is reduced before it is
ever collected, exactly as PLAN.md's "Ingest pipeline" describes:

    ingest.scan(path) -> LazyFrame -> user's filter/select -> plot(...)

`plot()` delegates its reduction to `engine.m4` / `engine.m4_numpy`.
`scatter()` and `histogram()` are not M4 (see their docstrings for why) but
follow the same shape: a lazy Polars aggregation on the LazyFrame path, a
plain numpy computation on the bare-array path.

Every command accepts two calling conventions, mirroring matplotlib's own
`ax.plot(x, y)` / `ax.plot('x', 'y', data=df)`:

- column names + `data=`: a LazyFrame or DataFrame (wrapped with `.lazy()`)
- array-like x/y directly: a list, numpy array, or Polars Series
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import polars as pl

from loupe_kernel import engine

ArrayLike = Sequence[float] | np.ndarray | pl.Series

DEFAULT_WIDTH = 1920  # PLAN.md's reference chart pixel width
DEFAULT_MAX_POINTS = 8000  # PLAN.md's "~2-8k aggregated points"
DEFAULT_BINS = 50
COLORMAPS = ("viridis", "plasma", "inferno", "coolwarm", "turbo")


@dataclass(frozen=True, eq=False)
class Figure:
    """A tier-1 plot, reduced and ready for the wire.

    For "line" and "scatter", x and y are equal-length point arrays. For
    "histogram", x holds `len(y) + 1` bin edges and y holds per-bin counts
    -- the numpy.histogram / matplotlib.pyplot.hist convention.

    `replot`, when set, redoes the same command's reduction for a new
    x_range (None meaning the full data range) -- what the kernel calls in
    response to a browser zoom/pan, so re-aggregation happens against the
    original source rather than the already-reduced points on screen. It
    is excluded from equality/repr: two Figures with the same data should
    still compare equal, and a closure's repr is useless noise.
    """

    kind: str
    x: np.ndarray
    y: np.ndarray
    title: str | None = None
    replot: Callable[[tuple[float, float] | None], "Figure"] | None = field(
        default=None, compare=False, repr=False
    )
    color: np.ndarray | None = None
    color_range: tuple[float, float] | None = None
    color_label: str | None = None
    cmap: str | None = None
    color_bins: int | None = None

    def __post_init__(self) -> None:
        if self.kind in ("line", "scatter", "heatmap"):
            if self.x.shape != self.y.shape:
                raise ValueError(
                    f"{self.kind} requires x and y of equal shape, "
                    f"got {self.x.shape} and {self.y.shape}"
                )
        elif self.kind == "histogram":
            if self.x.shape[0] != self.y.shape[0] + 1:
                raise ValueError(
                    f"histogram requires len(x) == len(y) + 1, "
                    f"got {self.x.shape[0]} and {self.y.shape[0]}"
                )
        else:
            raise ValueError(f"unknown Figure kind {self.kind!r}")
        if self.kind == "heatmap":
            if self.color is None or self.color.shape != self.x.shape:
                raise ValueError("heatmap requires matching x, y, and color shapes")

    def __len__(self) -> int:
        return self.y.shape[0]

    def __eq__(self, other: object) -> bool:
        # dataclass's auto-generated __eq__ (eq=False above suppresses it)
        # compares fields as a plain tuple, and tuple equality on a numpy
        # array raises ValueError ("truth value of an array is ambiguous")
        # rather than returning a bool -- so it never actually worked.
        if not isinstance(other, Figure):
            return NotImplemented
        return (
            self.kind == other.kind
            and self.title == other.title
            and np.array_equal(self.x, other.x)
            and np.array_equal(self.y, other.y)
            and np.array_equal(self.color, other.color)
            and self.color_range == other.color_range
            and self.color_label == other.color_label
            and self.cmap == other.cmap
            and self.color_bins == other.color_bins
        )


def _empty_figure(
    kind: str,
    title: str | None,
    replot: Callable[[tuple[float, float] | None], Figure] | None = None,
) -> Figure:
    x = np.zeros(1, dtype=np.float32) if kind == "histogram" else np.empty(0, dtype=np.float32)
    return Figure(kind, x, np.empty(0, dtype=np.float32), title, replot=replot)


def _as_lazyframe(data: pl.LazyFrame | pl.DataFrame) -> pl.LazyFrame:
    return data.lazy() if isinstance(data, pl.DataFrame) else data


def _as_f64_array(values: ArrayLike) -> np.ndarray:
    if isinstance(values, pl.Series):
        values = values.to_numpy()
    return np.asarray(values, dtype=np.float64)


def _require_columns(data: object, x: object, y: object | None = None) -> None:
    if data is not None and not isinstance(x, str):
        raise TypeError("x must be a column name (str) when data is given")
    if data is not None and y is not None and not isinstance(y, str):
        raise TypeError("y must be a column name (str) when data is given")
    if data is None and isinstance(x, str):
        raise TypeError("x must be array-like when data is not given")
    if data is None and isinstance(y, str):
        raise TypeError("y must be array-like when data is not given")


def plot(
    x: ArrayLike | str,
    y: ArrayLike | str,
    *,
    data: pl.LazyFrame | pl.DataFrame | None = None,
    width: int = DEFAULT_WIDTH,
    x_range: tuple[float, float] | None = None,
    title: str | None = None,
) -> Figure:
    """A line plot, reduced via M4 (`engine.m4` / `engine.m4_numpy`).

    - `plot("x", "y", data=lf)`: stays lazy through to the aggregated
      result -- the ingest-pipeline path.
    - `plot(x_array, y_array)`: delegates to `engine.m4_numpy`. Unlike that
      function, this sorts an unsorted `x` automatically before calling it
      -- a convenience worth the O(n log n) cost for the smaller,
      already-in-memory data this calling style implies. Call
      `engine.m4_numpy` directly to skip that check on data known sorted.
    """
    _require_columns(data, x, y)

    if data is not None:
        agg = engine.m4(_as_lazyframe(data), x, y, width, x_range)  # type: ignore[arg-type]
    else:
        xa, ya = _as_f64_array(x), _as_f64_array(y)
        if xa.shape != ya.shape:
            raise ValueError(f"x and y must have the same shape, got {xa.shape} and {ya.shape}")
        if xa.size > 1 and np.any(np.diff(xa) < 0):
            order = np.argsort(xa, kind="stable")
            xa, ya = xa[order], ya[order]
        agg = engine.m4_numpy(xa, ya, width, x_range)

    def replot(new_range: tuple[float, float] | None) -> Figure:
        return plot(x, y, data=data, width=width, x_range=new_range, title=title)

    return Figure("line", agg.x, agg.y, title, replot=replot)


def scatter(
    x: ArrayLike | str,
    y: ArrayLike | str,
    *,
    data: pl.LazyFrame | pl.DataFrame | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
    x_range: tuple[float, float] | None = None,
    title: str | None = None,
    seed: int = 0,
) -> Figure:
    """A scatter plot, thinned to at most `max_points`.

    M4 assumes a line tracing a function of x and does not apply to an
    unordered scatter, so the reduction here is sampling instead:

    - `data=` (LazyFrame/DataFrame): *systematic* sampling -- every Nth row
      after an optional viewport filter, where N comes from a single lazy
      row count. The source is never materialized just to be thinned.
    - array-like: genuine uniform random sampling via `numpy.random`,
      seeded for reproducibility -- affordable because the data is already
      in memory.
    """
    if max_points <= 0:
        raise ValueError(f"max_points must be positive, got {max_points}")
    _require_columns(data, x, y)

    def replot(new_range: tuple[float, float] | None) -> Figure:
        return scatter(
            x, y, data=data, max_points=max_points, x_range=new_range, title=title, seed=seed
        )

    if data is not None:
        lf = (
            _as_lazyframe(data)
            .select(x, y)
            .filter(
                pl.col(x).is_not_null()
                & pl.col(y).is_not_null()
                & pl.col(x).is_finite()
                & pl.col(y).is_finite()
            )
        )
        if x_range is not None:
            x0, x1 = x_range
            lf = lf.filter(pl.col(x).is_between(x0, x1))

        n = lf.select(pl.len()).collect().item()
        if n == 0:
            return _empty_figure("scatter", title, replot=replot)

        if n > max_points:
            step = -(-n // max_points)  # ceil division
            lf = lf.with_row_index("_i").filter(pl.col("_i") % step == 0).drop("_i")

        result = lf.collect()
        # Keep the wire format sorted by x so uPlot can find the nearest
        # point for its cursor and calculate scales reliably.
        result = result.sort(x)
        return Figure(
            "scatter", engine.to_f32(result[x]), engine.to_f32(result[y]), title, replot=replot
        )

    xa, ya = _as_f64_array(x), _as_f64_array(y)
    if xa.shape != ya.shape:
        raise ValueError(f"x and y must have the same shape, got {xa.shape} and {ya.shape}")

    finite = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[finite], ya[finite]

    if x_range is not None:
        x0, x1 = x_range
        mask = (xa >= x0) & (xa <= x1)
        xa, ya = xa[mask], ya[mask]

    if xa.size == 0:
        return _empty_figure("scatter", title, replot=replot)

    if xa.size > max_points:
        rng = np.random.default_rng(seed)
        idx = rng.choice(xa.size, size=max_points, replace=False)
        xa, ya = xa[idx], ya[idx]

    # uPlot's cursor and scale lookup require monotonically increasing x.
    # Random sampling preserves source order, which is not necessarily x
    # order (scatter inputs are commonly unsorted).
    order = np.argsort(xa, kind="stable")
    xa, ya = xa[order], ya[order]

    return Figure("scatter", xa.astype(np.float32), ya.astype(np.float32), title, replot=replot)


def heatmap(
    x: ArrayLike | str,
    y: ArrayLike | str,
    color: ArrayLike | str,
    *,
    data: pl.LazyFrame | pl.DataFrame | None = None,
    cmap: str = "viridis",
    bins: int = 8,
    max_points: int = DEFAULT_MAX_POINTS,
    x_range: tuple[float, float] | None = None,
    title: str | None = None,
) -> Figure:
    """Sampled scatter colored by a third numeric value, with equal-width bins.

    The finite source color range is fixed before viewport filtering/sampling.
    Zoom keeps that scale. Sampling is systematic and preserves complete triples;
    only scalar statistics and at most max_points rows are collected. Nulls,
    non-finite values, and values outside float32's range are omitted.
    """
    _require_columns(data, x, y)
    if cmap not in COLORMAPS:
        raise ValueError(f"cmap must be one of {', '.join(COLORMAPS)}")
    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= 16:
        raise ValueError("bins must be an integer between 2 and 16")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points <= 0:
        raise ValueError("max_points must be a positive integer")
    if data is not None:
        if not isinstance(data, (pl.LazyFrame, pl.DataFrame)):
            raise TypeError("data must be a Polars DataFrame or LazyFrame")
        if not isinstance(color, str):
            raise TypeError("color must be a column name when data is given")
        source = _as_lazyframe(data)
        schema = source.collect_schema()
        if any(not schema[name].is_numeric() for name in (x, y, color)):
            raise TypeError("heatmap requires numeric x, y, and color columns")
        source = source.select(
            pl.col(name).cast(pl.Float32).alias(alias)
            for name, alias in zip((x, y, color), ("_hx", "_hy", "_hc"))
        )
    else:
        if isinstance(color, str):
            raise TypeError("color must be array-like when data is not given")
        arrays = [_as_f64_array(values) for values in (x, y, color)]
        if any(a.ndim != 1 or a.shape != arrays[0].shape for a in arrays):
            raise ValueError("x, y, and color must be one-dimensional arrays of equal shape")
        source = pl.DataFrame(dict(zip(("_hx", "_hy", "_hc"), arrays))).lazy().cast(pl.Float32)
    source = source.filter(pl.all_horizontal(pl.all().is_not_null() & pl.all().is_finite()))
    bounds = source.select(pl.col("_hc").min().alias("lo"), pl.col("_hc").max().alias("hi")).collect(engine="streaming").row(0)
    limits = (float(bounds[0]), float(bounds[1])) if bounds[0] is not None else None

    def replot(new_range: tuple[float, float] | None) -> Figure:
        view = source
        if new_range is not None:
            if len(new_range) != 2 or not all(np.isfinite(v) for v in new_range) or new_range[0] >= new_range[1]:
                raise ValueError("x_range must contain two finite increasing values")
            view = view.filter(pl.col("_hx").is_between(*new_range))
        count = view.select(pl.len()).collect(engine="streaming").item()
        step = max(1, -(-count // max_points))
        result = (view.with_row_index("_row").filter(pl.col("_row") % step == 0)
                  .drop("_row").head(max_points).collect(engine="streaming").sort("_hx", maintain_order=True))
        return Figure(
            "heatmap", engine.to_f32(result["_hx"]), engine.to_f32(result["_hy"]), title,
            replot=replot, color=engine.to_f32(result["_hc"]), color_range=limits,
            color_label=color if isinstance(color, str) else "Color", cmap=cmap, color_bins=bins,
        )

    return replot(x_range)


def histogram(
    x: ArrayLike | str,
    *,
    data: pl.LazyFrame | pl.DataFrame | None = None,
    bins: int = DEFAULT_BINS,
    x_range: tuple[float, float] | None = None,
    title: str | None = None,
) -> Figure:
    """A histogram: `bins` per-bin counts and `bins + 1` bin edges.

    The `data=` path bins via a lazy group_by -- the same bucket-index
    technique as `engine.m4`, without the min/max/first/last reduction --
    so a 1e8-row source is reduced to `bins` rows before ever collecting.
    """
    if bins <= 0:
        raise ValueError(f"bins must be positive, got {bins}")
    _require_columns(data, x)

    def replot(new_range: tuple[float, float] | None) -> Figure:
        return histogram(x, data=data, bins=bins, x_range=new_range, title=title)

    if data is not None:
        lf = _as_lazyframe(data).select(x).filter(pl.col(x).is_not_null() & pl.col(x).is_finite())

        if x_range is None:
            bounds = lf.select(pl.col(x).min().alias("x0"), pl.col(x).max().alias("x1")).collect()
            if bounds.height == 0 or bounds["x0"][0] is None:
                return _empty_figure("histogram", title, replot=replot)
            x0, x1 = float(bounds["x0"][0]), float(bounds["x1"][0])
        else:
            x0, x1 = x_range
            lf = lf.filter(pl.col(x).is_between(x0, x1))

        counts = _lazy_bin_counts(lf, x, x0, x1, bins)
    else:
        arr = _as_f64_array(x)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return _empty_figure("histogram", title, replot=replot)

        if x_range is None:
            x0, x1 = float(arr.min()), float(arr.max())
        else:
            x0, x1 = x_range
            arr = arr[(arr >= x0) & (arr <= x1)]

        counts_i64, _ = np.histogram(arr, bins=bins, range=(x0, x1) if x1 > x0 else (x0, x0 + 1))
        counts = counts_i64.astype(np.float32)

    if x1 > x0:
        edges = np.linspace(x0, x1, bins + 1, dtype=np.float32)
    else:
        edges = np.full(bins + 1, x0, dtype=np.float32)

    return Figure("histogram", edges, counts, title, replot=replot)


def _lazy_bin_counts(lf: pl.LazyFrame, x: str, x0: float, x1: float, bins: int) -> np.ndarray:
    counts = np.zeros(bins, dtype=np.float32)
    span = x1 - x0

    if span <= 0:
        # A single distinct x value: everything that survived the filters
        # above lands in bin 0.
        counts[0] = lf.select(pl.len()).collect().item()
        return counts

    bin_idx = ((pl.col(x) - x0) * bins / span).floor().cast(pl.Int64).clip(0, bins - 1)
    agg = lf.with_columns(bin_idx.alias("_bin")).group_by("_bin").agg(pl.len().alias("count")).collect()
    if agg.height == 0:
        return counts

    idx = agg["_bin"].to_numpy()
    counts[idx] = agg["count"].to_numpy().astype(np.float32)
    return counts
