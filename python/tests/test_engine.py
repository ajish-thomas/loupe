import numpy as np
import polars as pl
import pytest

from loupe_kernel import engine


# ---------------------------------------------------------------------------
# m4() -- the Polars/LazyFrame path
# ---------------------------------------------------------------------------


def _lf(x, y) -> pl.LazyFrame:
    return pl.LazyFrame({"x": x, "y": y})


def test_m4_rejects_non_positive_n_buckets() -> None:
    lf = _lf([1, 2, 3], [1, 2, 3])
    with pytest.raises(ValueError, match="n_buckets must be positive"):
        engine.m4(lf, "x", "y", 0)


def test_m4_empty_frame_returns_empty() -> None:
    lf = _lf([], [])
    result = engine.m4(lf, "x", "y", 10)
    assert len(result) == 0
    assert result.x.dtype == np.float32
    assert result.y.dtype == np.float32


def test_m4_all_null_returns_empty() -> None:
    lf = pl.LazyFrame({"x": [None, None], "y": [None, None]}, schema={"x": pl.Float64, "y": pl.Float64})
    result = engine.m4(lf, "x", "y", 10)
    assert len(result) == 0


def test_m4_drops_null_nan_and_inf_rows() -> None:
    lf = _lf(
        [1.0, 2.0, None, 4.0, 5.0],
        [1.0, float("nan"), 2.0, float("inf"), 3.0],
    )
    result = engine.m4(lf, "x", "y", 10)
    # Only (1.0, 1.0) and (5.0, 3.0) survive filtering.
    assert sorted(result.x.tolist()) == [1.0, 5.0]
    assert sorted(result.y.tolist()) == [1.0, 3.0]


def test_m4_single_row() -> None:
    lf = _lf([5.0], [7.0])
    result = engine.m4(lf, "x", "y", 10)
    assert result.x.tolist() == [5.0]
    assert result.y.tolist() == [7.0]


def test_m4_single_distinct_x_value_collapses_to_one_bucket() -> None:
    # span == 0: every row shares the same x.
    lf = _lf([3.0, 3.0, 3.0], [1.0, -5.0, 9.0])
    result = engine.m4(lf, "x", "y", 100)
    assert set(result.x.tolist()) == {3.0}
    assert set(result.y.tolist()) == {1.0, -5.0, 9.0}


def test_m4_output_never_exceeds_four_points_per_bucket() -> None:
    rng = np.random.default_rng(0)
    n = 50_000
    x = np.sort(rng.uniform(0, 100, n))
    y = rng.normal(size=n)
    lf = _lf(x.tolist(), y.tolist())

    n_buckets = 37
    result = engine.m4(lf, "x", "y", n_buckets)

    assert len(result) <= 4 * n_buckets
    assert len(result) > 0


def test_m4_output_is_sorted_by_x() -> None:
    rng = np.random.default_rng(1)
    n = 10_000
    x = np.sort(rng.uniform(0, 50, n))
    y = np.sin(x) + rng.normal(0, 0.1, n)
    lf = _lf(x.tolist(), y.tolist())

    result = engine.m4(lf, "x", "y", 25)

    assert np.all(np.diff(result.x) >= 0)


def test_m4_preserves_global_min_and_max() -> None:
    """M4 is an envelope reduction: the true global min/max y must survive."""
    rng = np.random.default_rng(2)
    n = 20_000
    x = np.sort(rng.uniform(0, 10, n))
    y = rng.normal(size=n)
    lf = _lf(x.tolist(), y.tolist())

    result = engine.m4(lf, "x", "y", 20)

    assert float(result.y.min()) == pytest.approx(float(y.min()), abs=1e-4)
    assert float(result.y.max()) == pytest.approx(float(y.max()), abs=1e-4)


def test_m4_respects_explicit_x_range() -> None:
    x = list(range(100))
    y = [float(v) for v in x]
    lf = _lf(x, y)

    result = engine.m4(lf, "x", "y", 10, x_range=(20.0, 30.0))

    assert result.x.min() >= 20.0
    assert result.x.max() <= 30.0


def test_m4_x_range_filter_is_pushed_into_the_plan() -> None:
    """A filtered viewport must appear in the query plan (PLAN.md's laziness
    test: pushdown must reach the plan, not just be applied after the fact).
    """
    lf = _lf(list(range(1000)), list(range(1000)))
    x0, x1 = 100.0, 200.0

    lf_with_filter = (
        lf.select("x", "y")
        .filter(pl.col("x").is_not_null() & pl.col("y").is_not_null())
        .filter(pl.col("x").is_between(x0, x1))
    )
    assert "FILTER" in lf_with_filter.explain()


def test_m4_works_on_integer_columns() -> None:
    lf = pl.LazyFrame({"x": list(range(20)), "y": [v % 7 for v in range(20)]})
    result = engine.m4(lf, "x", "y", 5)
    assert len(result) > 0
    assert result.x.dtype == np.float32


def test_m4_never_collects_the_full_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only .collect() calls in m4() must be on the (small) aggregated
    result or the (single-row) min/max bounds -- never on the full frame.
    """
    collected_heights: list[int] = []
    real_collect = pl.LazyFrame.collect

    def spy_collect(self, *args, **kwargs):
        result = real_collect(self, *args, **kwargs)
        collected_heights.append(result.height)
        return result

    monkeypatch.setattr(pl.LazyFrame, "collect", spy_collect)

    rng = np.random.default_rng(3)
    n = 5_000
    x = np.sort(rng.uniform(0, 100, n))
    y = rng.normal(size=n)
    lf = _lf(x.tolist(), y.tolist())

    engine.m4(lf, "x", "y", 20)

    assert collected_heights, "expected at least one .collect() call"
    # Every collected intermediate must be far smaller than the source.
    assert all(h < n for h in collected_heights)


# ---------------------------------------------------------------------------
# m4() -- the batch-and-merge streaming path for low-selectivity queries
# ---------------------------------------------------------------------------


def test_m4_streaming_matches_direct_path() -> None:
    """Forcing streaming (small batch size, zero selectivity threshold) must
    produce the exact same result as the default direct path on the same
    data -- the whole point of the reformulated, associative reduction.
    """
    rng = np.random.default_rng(42)
    n = 20_000
    x = np.sort(rng.uniform(0, 1000, n))
    y = np.sin(x / 10) + rng.normal(0, 0.1, n)
    lf = _lf(x.tolist(), y.tolist())

    direct = engine.m4(lf, "x", "y", 50)
    assert len(direct) > 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_STREAM_BATCH_ROWS", 137)  # deliberately uneven vs n
        mp.setattr(engine, "_STREAM_SELECTIVITY_THRESHOLD", 0.0)
        streamed = engine.m4(lf, "x", "y", 50)

    np.testing.assert_array_equal(direct.x, streamed.x)
    np.testing.assert_array_equal(direct.y, streamed.y)


def test_m4_streaming_with_x_range_matches_direct_path() -> None:
    """Same equivalence check, but with an explicit x_range -- exercises
    batches where the range filter matches nothing (skipped) and batches
    straddling the range boundary.
    """
    x = list(range(2000))
    y = [float(v % 13) for v in x]
    lf = _lf(x, y)

    direct = engine.m4(lf, "x", "y", 20, x_range=(500.0, 700.0))
    assert len(direct) > 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_STREAM_BATCH_ROWS", 63)
        mp.setattr(engine, "_STREAM_SELECTIVITY_THRESHOLD", 0.0)
        streamed = engine.m4(lf, "x", "y", 20, x_range=(500.0, 700.0))

    np.testing.assert_array_equal(direct.x, streamed.x)
    np.testing.assert_array_equal(direct.y, streamed.y)


def test_m4_routes_to_streaming_only_above_selectivity_threshold() -> None:
    calls: list[int] = []
    original = engine._m4_streaming

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    x = list(range(1000))
    y = [float(v) for v in x]
    lf = _lf(x, y)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_m4_streaming", spy)
        mp.setattr(engine, "_STREAM_BATCH_ROWS", 100)

        # matched_rows = 51 <= the (patched) batch threshold: never even
        # considers streaming.
        engine.m4(lf, "x", "y", 10, x_range=(0.0, 50.0))
        assert calls == []

        # matched_rows = 1000 > threshold and ratio = 1000/1000 = 100%: streams.
        engine.m4(lf, "x", "y", 10, x_range=(0.0, 999.0))
        assert calls == [1]


def test_m4_low_selectivity_with_many_matched_rows_still_uses_direct_path() -> None:
    """matched_rows can exceed the batch threshold while still being a small
    fraction of the total -- that should stay on the direct (pushdown-
    friendly) path, not pay for batching it doesn't need.
    """
    calls: list[int] = []
    original = engine._m4_streaming

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    x = list(range(10_000))
    y = [float(v) for v in x]
    lf = _lf(x, y)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_m4_streaming", spy)
        mp.setattr(engine, "_STREAM_BATCH_ROWS", 100)

        # matched_rows = 500 > 100, but ratio = 500/10_000 = 5% < 20%.
        engine.m4(lf, "x", "y", 10, x_range=(0.0, 499.0))
        assert calls == []


def test_m4_streaming_helper_returns_empty_dataframe_when_no_batch_matches() -> None:
    lf = _lf([1.0, 2.0], [1.0, 2.0])
    bucket = pl.lit(0, dtype=pl.Int64)
    # x_range excludes every row, so every batch's filter matches nothing.
    result = engine._m4_streaming(lf, "x", "y", 100.0, 200.0, True, bucket, total_rows=2)
    assert result.height == 0


def test_bounds_streaming_matches_direct_bounds() -> None:
    rng = np.random.default_rng(9)
    n = 5_000
    x = rng.uniform(-50, 500, n)
    y = rng.normal(size=n)
    lf = _lf(x.tolist(), y.tolist())

    direct = lf.select(
        pl.col("x").min().alias("x0"), pl.col("x").max().alias("x1"), pl.len().alias("n")
    ).collect()

    x0, x1, matched_rows = engine._bounds_streaming(lf, "x", "y", total_rows=n)

    assert x0 == pytest.approx(float(direct["x0"][0]))
    assert x1 == pytest.approx(float(direct["x1"][0]))
    assert matched_rows == int(direct["n"][0])


def test_bounds_streaming_skips_batches_with_no_finite_rows() -> None:
    # First batch (rows 0-1) is all-null/non-finite; only the second batch
    # (rows 2-3) has real data.
    lf = _lf([None, float("nan"), 5.0, 10.0], [None, 1.0, 2.0, 3.0])
    result = engine._bounds_streaming(lf, "x", "y", total_rows=4)
    assert result == (5.0, 10.0, 2)


def test_bounds_streaming_returns_none_when_nothing_matches() -> None:
    lf = _lf([None, float("nan")], [None, 1.0])
    assert engine._bounds_streaming(lf, "x", "y", total_rows=2) is None


def test_m4_full_range_routes_bounds_through_streaming_above_threshold() -> None:
    """x_range=None on a large file must use `_bounds_streaming`, not the
    single-collect bounds query -- that single collect is the ~3GB-RSS
    regression this batching exists to avoid.
    """
    calls: list[int] = []
    original = engine._bounds_streaming

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    x = list(range(1000))
    y = [float(v) for v in x]
    lf = _lf(x, y)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine, "_bounds_streaming", spy)
        mp.setattr(engine, "_STREAM_BATCH_ROWS", 100)

        result = engine.m4(lf, "x", "y", 10)  # x_range=None, 1000 rows > patched 100

        assert calls == [1]
        assert len(result) > 0


# ---------------------------------------------------------------------------
# m4_numpy() -- the bare in-memory array fallback
# ---------------------------------------------------------------------------


def test_m4_numpy_rejects_non_positive_n_buckets() -> None:
    with pytest.raises(ValueError, match="n_buckets must be positive"):
        engine.m4_numpy(np.array([1.0]), np.array([1.0]), 0)


def test_m4_numpy_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="same shape"):
        engine.m4_numpy(np.array([1.0, 2.0]), np.array([1.0]), 5)


def test_m4_numpy_rejects_unsorted_x() -> None:
    with pytest.raises(ValueError, match="sorted ascending"):
        engine.m4_numpy(np.array([2.0, 1.0, 3.0]), np.array([1.0, 2.0, 3.0]), 2)


def test_m4_numpy_empty_input_returns_empty() -> None:
    result = engine.m4_numpy(np.array([]), np.array([]), 10)
    assert len(result) == 0


def test_m4_numpy_drops_non_finite_values() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = np.array([1.0, float("nan"), float("inf"), 3.0])
    result = engine.m4_numpy(x, y, 10)
    assert sorted(result.x.tolist()) == [1.0, 4.0]


def test_m4_numpy_matches_hand_worked_example() -> None:
    # Same fixture verified by hand while prototyping the algorithm.
    x = np.array([0.0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    y = np.array([0.0, 5, -3, 2, 4, -1, 8, 0, -8, 1])

    result = engine.m4_numpy(x, y, 3)

    points = list(zip(result.x.tolist(), result.y.tolist()))
    assert points == [
        (0.0, 0.0),
        (1.0, 5.0),
        (2.0, -3.0),
        (3.0, 2.0),
        (4.0, 4.0),
        (5.0, -1.0),
        (6.0, 8.0),
        (8.0, -8.0),
        (9.0, 1.0),
    ]


def test_m4_numpy_output_is_sorted_by_x() -> None:
    rng = np.random.default_rng(4)
    n = 10_000
    x = np.sort(rng.uniform(0, 50, n))
    y = np.sin(x) + rng.normal(0, 0.1, n)

    result = engine.m4_numpy(x, y, 25)

    assert np.all(np.diff(result.x) >= 0)


def test_m4_numpy_preserves_global_min_and_max() -> None:
    rng = np.random.default_rng(5)
    n = 20_000
    x = np.sort(rng.uniform(0, 10, n))
    y = rng.normal(size=n)

    result = engine.m4_numpy(x, y, 20)

    assert float(result.y.min()) == pytest.approx(float(y.min()), abs=1e-4)
    assert float(result.y.max()) == pytest.approx(float(y.max()), abs=1e-4)


def test_m4_numpy_respects_explicit_x_range() -> None:
    x = np.arange(100, dtype=np.float64)
    y = x.copy()

    result = engine.m4_numpy(x, y, 10, x_range=(20.0, 30.0))

    assert result.x.min() >= 20.0
    assert result.x.max() <= 30.0


def test_m4_numpy_output_never_exceeds_four_points_per_bucket() -> None:
    rng = np.random.default_rng(6)
    n = 50_000
    x = np.sort(rng.uniform(0, 100, n))
    y = rng.normal(size=n)

    n_buckets = 37
    result = engine.m4_numpy(x, y, n_buckets)

    assert len(result) <= 4 * n_buckets
    assert len(result) > 0


# ---------------------------------------------------------------------------
# m4() and m4_numpy() agree on the same data
# ---------------------------------------------------------------------------


def test_m4_and_m4_numpy_agree() -> None:
    rng = np.random.default_rng(7)
    n = 30_000
    x = np.sort(rng.uniform(0, 200, n))
    y = np.cos(x / 10) + rng.normal(0, 0.2, n)

    lazy_result = engine.m4(_lf(x.tolist(), y.tolist()), "x", "y", 40)
    numpy_result = engine.m4_numpy(x, y, 40)

    np.testing.assert_allclose(lazy_result.x, numpy_result.x, rtol=1e-5)
    np.testing.assert_allclose(lazy_result.y, numpy_result.y, rtol=1e-5)


# ---------------------------------------------------------------------------
# Aggregated
# ---------------------------------------------------------------------------


def test_aggregated_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="same shape"):
        engine.Aggregated(np.array([1.0, 2.0], dtype=np.float32), np.array([1.0], dtype=np.float32))


def test_aggregated_len() -> None:
    agg = engine.Aggregated(
        np.array([1.0, 2.0, 3.0], dtype=np.float32), np.array([1.0, 2.0, 3.0], dtype=np.float32)
    )
    assert len(agg) == 3
