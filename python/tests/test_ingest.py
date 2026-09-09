from pathlib import Path

import polars as pl
import pytest

from loupe_kernel import ingest

SAMPLE = pl.DataFrame({"x": [1, 2, 3], "y": [4.0, 5.0, 6.0]})


def test_scan_csv_returns_lazyframe_with_expected_schema(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    SAMPLE.write_csv(path)

    lf = ingest.scan(path)

    assert isinstance(lf, pl.LazyFrame)
    assert lf.collect_schema().names() == ["x", "y"]


def test_scan_parquet_returns_lazyframe(tmp_path: Path) -> None:
    path = tmp_path / "data.parquet"
    SAMPLE.write_parquet(path)

    lf = ingest.scan(path)

    assert isinstance(lf, pl.LazyFrame)
    assert lf.collect().equals(SAMPLE)


def test_scan_ndjson_returns_lazyframe(tmp_path: Path) -> None:
    path = tmp_path / "data.ndjson"
    SAMPLE.write_ndjson(path)

    lf = ingest.scan(path)

    assert isinstance(lf, pl.LazyFrame)
    assert lf.collect().equals(SAMPLE)


def test_scan_ipc_returns_lazyframe(tmp_path: Path) -> None:
    path = tmp_path / "data.arrow"
    SAMPLE.write_ipc(path)

    lf = ingest.scan(path)

    assert isinstance(lf, pl.LazyFrame)
    assert lf.collect().equals(SAMPLE)


def test_scan_unsupported_extension_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.xyz"
    path.write_text("not a real data file")

    with pytest.raises(ValueError, match="unsupported file extension"):
        ingest.scan(path)


def test_scan_does_not_collect(tmp_path: Path) -> None:
    """Ingest must never materialize the source (PLAN.md, "Ingest pipeline")."""
    path = tmp_path / "data.csv"
    SAMPLE.write_csv(path)

    lf = ingest.scan(path)

    # A LazyFrame's plan is inspectable without executing it; explain()
    # must succeed without ever calling .collect().
    assert "CSV" in lf.explain() or "Csv" in lf.explain()


def test_large_csv_is_cached_and_reused(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "data.csv"
    SAMPLE.write_csv(path)
    cache = tmp_path / "cache"
    lf = ingest.scan(path, cache_threshold=0, cache_dir=cache)
    assert "Parquet" in lf.explain()
    assert lf.collect().equals(SAMPLE)
    assert len(list(cache.glob("*.parquet"))) == 1

    def no_rewrite(*args, **kwargs):
        raise AssertionError("unchanged CSV was parsed again")

    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", no_rewrite)
    assert ingest.scan(path, cache_threshold=0, cache_dir=cache).collect().equals(SAMPLE)


def test_cache_invalidates_on_source_or_options_change(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    cache = tmp_path / "cache"
    path.write_text("x\n1\n")
    first = ingest.scan(path, cache_threshold=0, cache_dir=cache)
    path.write_text("x\n200\n")
    second = ingest.scan(path, cache_threshold=0, cache_dir=cache)
    strings = ingest.scan(path, cache_threshold=0, cache_dir=cache, schema_overrides={"x": pl.String})
    assert first.collect()["x"].to_list() == [1]
    assert second.collect()["x"].to_list() == [200]
    assert strings.collect()["x"].to_list() == ["200"]
    assert len(list(cache.glob("*.parquet"))) == 3


def test_failed_csv_conversion_leaves_no_cache_entry(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("x\n1\nbad\n")
    cache = tmp_path / "cache"
    with pytest.raises(pl.exceptions.ComputeError):
        ingest.scan(path, infer_schema_length=1, cache_threshold=0, cache_dir=cache)
    assert list(cache.iterdir()) == []
    lf = ingest.scan(path, schema_overrides={"x": pl.String}, cache_threshold=0, cache_dir=cache)
    assert lf.collect()["x"].to_list() == ["1", "bad"]


def test_changed_source_during_cache_write_is_rejected(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "data.csv"
    SAMPLE.write_csv(path)
    sink = pl.LazyFrame.sink_parquet

    def changing_sink(lf, target, **kwargs):
        sink(lf, target, **kwargs)
        path.write_text("x,y\n1,2\n")

    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", changing_sink)
    cache = tmp_path / "cache"
    with pytest.raises(OSError, match="source changed"):
        ingest.scan(path, cache_threshold=0, cache_dir=cache)
    assert list(cache.iterdir()) == []


def test_csv_dates_and_schema_feedback(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("date,value\n2026-09-08,1\n")
    events = []
    with ingest.report_scans(lambda path, schema: events.append((path, schema))):
        lf = ingest.scan(path)
    assert events == [(path, pl.Schema({"date": pl.Date, "value": pl.Int64}))]
    assert lf.collect_schema()["date"] == pl.Date
    ingest.scan(path)
    assert len(events) == 1  # observer does not leak into the next request


@pytest.mark.parametrize("n", [0, -1, 101, True, 1.5, None])
def test_preview_rejects_unbounded_or_invalid_limits(n) -> None:
    with pytest.raises(ValueError, match="preview rows"):
        ingest.preview(SAMPLE.lazy(), n)


def test_preview_collects_only_a_capped_plan(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "data.parquet"
    pl.DataFrame({"x": range(1000)}).write_parquet(path)
    original = pl.LazyFrame.collect
    collected = []

    def bounded_collect(lf, *args, **kwargs):
        result = original(lf, *args, **kwargs)
        collected.append(result.height)
        assert result.height <= 21
        return result

    monkeypatch.setattr(pl.LazyFrame, "collect", bounded_collect)
    result = ingest.preview(ingest.scan(path).filter(pl.col("x") >= 10))
    assert collected == [21]
    assert result.data["x"].to_list() == list(range(10, 30))
    assert result.has_more


@pytest.mark.parametrize("rows,has_more", [(0, False), (20, False), (21, True)])
def test_preview_truncation_boundary(rows: int, has_more: bool) -> None:
    result = ingest.preview(pl.DataFrame({"x": range(rows)}))
    assert result.data.height == min(20, rows)
    assert result.has_more is has_more


def test_small_scan_and_schema_never_collect(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "data.csv"
    SAMPLE.write_csv(path)

    def fail_collect(*args, **kwargs):
        raise AssertionError("scan collected source rows")

    monkeypatch.setattr(pl.LazyFrame, "collect", fail_collect)
    with ingest.report_scans(lambda path, schema: None):
        assert isinstance(ingest.scan(path), pl.LazyFrame)
