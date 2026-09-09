from pathlib import Path

import numpy as np
import polars as pl
import pytest

from loupe_kernel import api, ingest


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------


def test_figure_line_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="equal shape"):
        api.Figure("line", np.zeros(3, dtype=np.float32), np.zeros(2, dtype=np.float32))


def test_figure_scatter_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="equal shape"):
        api.Figure("scatter", np.zeros(3, dtype=np.float32), np.zeros(2, dtype=np.float32))


def test_figure_histogram_requires_one_more_edge_than_count() -> None:
    with pytest.raises(ValueError, match="len\\(x\\) == len\\(y\\) \\+ 1"):
        api.Figure("histogram", np.zeros(5, dtype=np.float32), np.zeros(5, dtype=np.float32))


def test_figure_histogram_accepts_correct_shapes() -> None:
    fig = api.Figure("histogram", np.zeros(6, dtype=np.float32), np.zeros(5, dtype=np.float32))
    assert len(fig) == 5


def test_figure_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown Figure kind"):
        api.Figure("pie", np.zeros(1, dtype=np.float32), np.zeros(1, dtype=np.float32))


def test_figure_len_is_y_length() -> None:
    fig = api.Figure("line", np.zeros(7, dtype=np.float32), np.zeros(7, dtype=np.float32))
    assert len(fig) == 7


# ---------------------------------------------------------------------------
# plot()
# ---------------------------------------------------------------------------


def test_plot_array_requires_data_absent() -> None:
    with pytest.raises(TypeError, match="array-like"):
        api.plot("x", "y")


def test_plot_column_names_require_data() -> None:
    with pytest.raises(TypeError, match="array-like"):
        api.plot("x", "y", data=None)


def test_plot_data_requires_column_name_strings() -> None:
    lf = pl.LazyFrame({"x": [1.0, 2.0], "y": [1.0, 2.0]})
    with pytest.raises(TypeError, match="column name"):
        api.plot([1.0, 2.0], [1.0, 2.0], data=lf)


def test_plot_array_path_returns_line_figure() -> None:
    x = np.linspace(0, 10, 1000)
    y = np.sin(x)
    fig = api.plot(x, y, width=20)
    assert fig.kind == "line"
    assert len(fig) > 0
    assert len(fig) <= 4 * 20


def test_plot_array_path_auto_sorts_unsorted_x() -> None:
    """Unlike engine.m4_numpy, api.plot()'s array path is forgiving of
    unsorted input -- it sorts before delegating."""
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, 500)
    y = np.sin(x)
    order = rng.permutation(x.size)

    fig = api.plot(x[order], y[order], width=15)

    assert np.all(np.diff(fig.x) >= 0)


def test_plot_lazyframe_path_returns_line_figure() -> None:
    x = np.linspace(0, 10, 1000)
    y = np.sin(x)
    lf = pl.LazyFrame({"x": x, "y": y})

    fig = api.plot("x", "y", data=lf, width=20)

    assert fig.kind == "line"
    assert len(fig) > 0


def test_plot_dataframe_is_accepted_via_lazy() -> None:
    df = pl.DataFrame({"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]})
    fig = api.plot("x", "y", data=df, width=5)
    assert fig.kind == "line"
    assert len(fig) > 0


def test_plot_x_range_narrows_output() -> None:
    x = np.arange(100, dtype=np.float64)
    y = x.copy()
    lf = pl.LazyFrame({"x": x, "y": y})

    fig = api.plot("x", "y", data=lf, width=10, x_range=(20.0, 30.0))

    assert fig.x.min() >= 20.0
    assert fig.x.max() <= 30.0


def test_plot_title_is_carried_through() -> None:
    fig = api.plot([1.0, 2.0], [1.0, 2.0], title="My Plot")
    assert fig.title == "My Plot"


# ---------------------------------------------------------------------------
# scatter()
# ---------------------------------------------------------------------------


def test_scatter_rejects_non_positive_max_points() -> None:
    with pytest.raises(ValueError, match="max_points must be positive"):
        api.scatter([1.0], [1.0], max_points=0)


def test_scatter_array_path_thins_to_max_points() -> None:
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 100, 10_000)
    y = rng.normal(size=10_000)

    fig = api.scatter(x, y, max_points=500)

    assert fig.kind == "scatter"
    assert len(fig) == 500


@pytest.mark.parametrize("lazy", [False, True])
def test_scatter_sorts_pairs_after_sampling_and_repeated_zoom(lazy: bool) -> None:
    x = np.random.default_rng(4).normal(size=100_000)
    y = x * 0.5 + np.random.default_rng(5).normal(size=100_000)
    source = set(zip(x.astype(np.float32), y.astype(np.float32)))
    fig = (api.scatter("x", "y", data=pl.LazyFrame({"x": x, "y": y}), max_points=8000)
           if lazy else api.scatter(x, y, max_points=8000))
    for viewport in [None, (-1.0, 1.0), (-0.2, 0.2), (-0.02, 0.02), None]:
        fig = fig.replot(viewport)
        assert 0 < len(fig) <= 8000
        assert np.all(np.diff(fig.x) >= 0)
        assert set(zip(fig.x, fig.y)) <= source  # sorting must preserve x/y pairs
        if viewport is not None:
            assert fig.x.min() >= viewport[0]
            assert fig.x.max() <= viewport[1]


def test_scatter_array_path_under_budget_keeps_everything() -> None:
    x = np.array([1.0, 2.0, 3.0])
    y = np.array([1.0, 2.0, 3.0])

    fig = api.scatter(x, y, max_points=100)

    assert len(fig) == 3


def test_scatter_array_path_is_deterministic_given_seed() -> None:
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 100, 10_000)
    y = rng.normal(size=10_000)

    fig_a = api.scatter(x, y, max_points=200, seed=42)
    fig_b = api.scatter(x, y, max_points=200, seed=42)

    np.testing.assert_array_equal(fig_a.x, fig_b.x)
    np.testing.assert_array_equal(fig_a.y, fig_b.y)


def test_scatter_array_path_drops_non_finite() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = np.array([1.0, float("nan"), float("inf"), 3.0])

    fig = api.scatter(x, y, max_points=100)

    assert len(fig) == 2


def test_scatter_array_path_respects_x_range() -> None:
    x = np.arange(100, dtype=np.float64)
    y = x.copy()

    fig = api.scatter(x, y, max_points=1000, x_range=(20.0, 30.0))

    assert fig.x.min() >= 20.0
    assert fig.x.max() <= 30.0


def test_scatter_array_path_empty_input() -> None:
    fig = api.scatter(np.array([]), np.array([]), max_points=10)
    assert len(fig) == 0


def test_scatter_lazyframe_path_thins_to_at_most_max_points() -> None:
    lf = pl.LazyFrame({"x": list(range(10_000)), "y": list(range(10_000))})

    fig = api.scatter("x", "y", data=lf, max_points=500)

    assert fig.kind == "scatter"
    assert 0 < len(fig) <= 500


def test_scatter_lazyframe_path_under_budget_keeps_everything() -> None:
    lf = pl.LazyFrame({"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]})

    fig = api.scatter("x", "y", data=lf, max_points=100)

    assert len(fig) == 3


def test_scatter_lazyframe_path_empty_input() -> None:
    lf = pl.LazyFrame({"x": [], "y": []}, schema={"x": pl.Float64, "y": pl.Float64})
    fig = api.scatter("x", "y", data=lf, max_points=10)
    assert len(fig) == 0


def test_scatter_lazyframe_path_respects_x_range() -> None:
    lf = pl.LazyFrame({"x": list(range(100)), "y": list(range(100))})

    fig = api.scatter("x", "y", data=lf, max_points=1000, x_range=(20.0, 30.0))

    assert fig.x.min() >= 20.0
    assert fig.x.max() <= 30.0


def test_scatter_lazyframe_and_array_paths_agree_on_row_count() -> None:
    """Both paths should thin to (approximately) the same output size for
    the same input and budget."""
    x = list(range(10_000))
    y = list(range(10_000))

    fig_lazy = api.scatter("x", "y", data=pl.LazyFrame({"x": x, "y": y}), max_points=300)
    fig_array = api.scatter(np.array(x, dtype=np.float64), np.array(y, dtype=np.float64), max_points=300)

    assert len(fig_lazy) <= 300
    assert len(fig_array) == 300


# ---------------------------------------------------------------------------
# histogram()
# ---------------------------------------------------------------------------


def test_histogram_rejects_non_positive_bins() -> None:
    with pytest.raises(ValueError, match="bins must be positive"):
        api.histogram([1.0], bins=0)


def test_histogram_array_path_shapes() -> None:
    x = np.linspace(0, 10, 1000)
    fig = api.histogram(x, bins=10)
    assert fig.kind == "histogram"
    assert len(fig.x) == 11
    assert len(fig.y) == 10


def test_histogram_array_path_counts_sum_to_input_size() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=5000)
    fig = api.histogram(x, bins=25)
    assert fig.y.sum() == 5000


def test_histogram_array_path_drops_non_finite() -> None:
    x = np.array([1.0, 2.0, float("nan"), float("inf"), 3.0])
    fig = api.histogram(x, bins=5)
    assert fig.y.sum() == 3


def test_histogram_array_path_empty_input() -> None:
    fig = api.histogram(np.array([]), bins=5)
    assert len(fig) == 0
    assert len(fig.x) == 1


def test_histogram_array_path_single_distinct_value() -> None:
    fig = api.histogram(np.array([5.0, 5.0, 5.0]), bins=5)
    assert fig.y.sum() == 3
    assert fig.y[0] == 3


def test_histogram_lazyframe_path_shapes_and_total() -> None:
    lf = pl.LazyFrame({"x": list(range(1000))})
    fig = api.histogram("x", data=lf, bins=10)
    assert len(fig.x) == 11
    assert len(fig.y) == 10
    assert fig.y.sum() == 1000


def test_histogram_lazyframe_path_matches_array_path() -> None:
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 100, 5000)

    fig_lazy = api.histogram("x", data=pl.LazyFrame({"x": x}), bins=20)
    fig_array = api.histogram(x, bins=20)

    np.testing.assert_allclose(fig_lazy.x, fig_array.x, rtol=1e-5)
    np.testing.assert_array_equal(fig_lazy.y, fig_array.y)


def test_histogram_lazyframe_path_respects_x_range() -> None:
    lf = pl.LazyFrame({"x": list(range(100))})
    fig = api.histogram("x", data=lf, bins=5, x_range=(0.0, 50.0))
    # is_between is inclusive on both ends, matching engine.m4: 0..50 is 51 values.
    assert fig.y.sum() == 51


def test_histogram_lazyframe_path_all_null_returns_empty() -> None:
    lf = pl.LazyFrame({"x": [None, None]}, schema={"x": pl.Float64})
    fig = api.histogram("x", data=lf, bins=5)
    assert len(fig) == 0


def test_histogram_data_requires_column_name_string() -> None:
    lf = pl.LazyFrame({"x": [1.0, 2.0]})
    with pytest.raises(TypeError, match="column name"):
        api.histogram([1.0, 2.0], data=lf)


# ---------------------------------------------------------------------------
# End to end: ingest.scan() -> user filter -> api.plot()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Figure.replot -- server-side re-aggregation for a browser zoom/pan
# ---------------------------------------------------------------------------


def test_plot_array_path_replot_narrows_to_new_range() -> None:
    x = np.arange(1000, dtype=np.float64)
    y = x.copy()

    fig = api.plot(x, y, width=50)
    assert fig.replot is not None

    zoomed = fig.replot((100.0, 200.0))

    assert zoomed.kind == "line"
    assert zoomed.x.min() >= 100.0
    assert zoomed.x.max() <= 200.0


def test_plot_array_path_replot_none_restores_full_range() -> None:
    x = np.arange(1000, dtype=np.float64)
    y = x.copy()

    fig = api.plot(x, y, width=50)
    zoomed = fig.replot((100.0, 200.0))
    restored = zoomed.replot(None)

    assert restored.x.min() == pytest.approx(0.0)
    assert restored.x.max() == pytest.approx(999.0, abs=1.0)


def test_plot_lazyframe_path_replot_narrows_to_new_range() -> None:
    x = np.arange(1000, dtype=np.float64)
    lf = pl.LazyFrame({"x": x, "y": x})

    fig = api.plot("x", "y", data=lf, width=50)
    zoomed = fig.replot((100.0, 200.0))

    assert zoomed.x.min() >= 100.0
    assert zoomed.x.max() <= 200.0


def test_plot_replot_preserves_title() -> None:
    fig = api.plot([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], title="my title")
    zoomed = fig.replot((1.0, 2.0))
    assert zoomed.title == "my title"


def test_scatter_replot_respects_new_range_and_max_points() -> None:
    x = np.arange(1000, dtype=np.float64)
    y = x.copy()

    fig = api.scatter(x, y, max_points=50)
    zoomed = fig.replot((100.0, 200.0))

    assert zoomed.kind == "scatter"
    assert zoomed.x.min() >= 100.0
    assert zoomed.x.max() <= 200.0
    assert len(zoomed) <= 50


def test_scatter_lazyframe_replot_narrows_to_new_range() -> None:
    lf = pl.LazyFrame({"x": list(range(1000)), "y": list(range(1000))})

    fig = api.scatter("x", "y", data=lf, max_points=50)
    zoomed = fig.replot((100.0, 200.0))

    assert zoomed.x.min() >= 100.0
    assert zoomed.x.max() <= 200.0


def test_histogram_replot_rebins_new_range() -> None:
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 100, 5000)

    fig = api.histogram(x, bins=10)
    zoomed = fig.replot((0.0, 50.0))

    assert zoomed.kind == "histogram"
    assert zoomed.x.min() == pytest.approx(0.0)
    assert zoomed.x.max() == pytest.approx(50.0)


def test_histogram_lazyframe_replot_narrows_to_new_range() -> None:
    lf = pl.LazyFrame({"x": list(range(100))})

    fig = api.histogram("x", data=lf, bins=5)
    zoomed = fig.replot((0.0, 50.0))

    assert zoomed.y.sum() == 51  # is_between is inclusive, as elsewhere


def test_empty_figure_replot_can_recover_data_in_a_different_range() -> None:
    """An initial out-of-data x_range legitimately returns an empty figure;
    zooming to a range that does have data must still work."""
    x = np.arange(100, dtype=np.float64)
    y = x.copy()

    fig = api.plot(x, y, x_range=(500.0, 600.0))  # no data in this range
    assert len(fig) == 0
    assert fig.replot is not None

    recovered = fig.replot((0.0, 50.0))
    assert len(recovered) > 0


def test_figure_equality_ignores_replot() -> None:
    fig_a = api.plot([1.0, 2.0], [1.0, 2.0])
    fig_b = api.plot([1.0, 2.0], [1.0, 2.0])
    assert fig_a.replot is not fig_b.replot  # distinct closures
    assert fig_a == fig_b  # but the Figures still compare equal


def test_figure_repr_does_not_include_replot_closure() -> None:
    fig = api.plot([1.0, 2.0], [1.0, 2.0])
    assert "replot" not in repr(fig)


def test_ingest_filter_plot_pipeline(tmp_path: Path) -> None:
    """The full tier-1 pipeline PLAN.md describes: scan a file lazily,
    filter it, and plot -- without ever collecting the unfiltered source.
    """
    n = 10_000
    x = np.arange(n, dtype=np.float64)
    y = np.sin(x / 100)
    path = tmp_path / "data.csv"
    pl.DataFrame({"x": x, "y": y}).write_csv(path)

    lf = ingest.scan(path).filter(pl.col("x") < 5000)
    fig = api.plot("x", "y", data=lf, width=50)

    assert fig.kind == "line"
    assert fig.x.max() < 5000
    assert len(fig) <= 4 * 50
