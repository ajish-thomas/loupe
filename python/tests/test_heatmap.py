import sys
import numpy as np
import polars as pl
import pytest

from loupe_kernel.api import heatmap


@pytest.mark.parametrize('lazy', [True, False])
def test_triples_scale_and_zoom(lazy):
    x = np.arange(1000)[::-1]
    df = pl.DataFrame({'x': x, 'y': x * 2, 'irdrop': x * 3})
    fig = (heatmap('x', 'y', 'irdrop', data=df.lazy(), max_points=31, cmap='plasma')
           if lazy else heatmap(x, x * 2, x * 3, max_points=31, cmap='plasma'))
    for viewport in [(100, 200), (5000, 6000), None]:
        fig = fig.replot(viewport)
        assert len(fig) <= 31
        assert np.all(np.diff(fig.x) >= 0)
        np.testing.assert_array_equal(fig.y, fig.x * 2)
        np.testing.assert_array_equal(fig.color, fig.x * 3)
        assert fig.color_range == (0, 2997)
        assert fig.cmap == 'plasma'
        if viewport == (5000, 6000):
            assert len(fig) == 0
    assert 'matplotlib' not in sys.modules


@pytest.mark.parametrize('values,limits,length', [([], None, 0), ([5, 5], (5, 5), 2), ([float('nan'), float('inf')], None, 0)])
def test_empty_constant_and_nonfinite(values, limits, length):
    fig = heatmap(range(len(values)), range(len(values)), values)
    assert fig.color_range == limits
    assert len(fig) == length


def test_nulls_and_same_column_aliases():
    df = pl.DataFrame({'v': [None, 1., float('inf'), 2.]})
    fig = heatmap('v', 'v', 'v', data=df)
    assert fig.color.tolist() == [1, 2]


@pytest.mark.parametrize('kwargs', [{'bins': 0}, {'bins': 17}, {'bins': True}, {'max_points': 0}, {'max_points': 1.5}, {'cmap': 'unknown'}, {'x_range': (2,1)}, {'x_range': (0,float('nan'))}])
def test_invalid_options(kwargs):
    with pytest.raises(ValueError):
        heatmap([1], [2], [3], **kwargs)


def test_bad_shapes_and_types():
    with pytest.raises(ValueError):
        heatmap([1], [2], [3, 4])
    with pytest.raises(ValueError):
        heatmap([[1]], [[2]], [[3]])
    with pytest.raises(TypeError):
        heatmap('x', 'y', 'c', data=pl.DataFrame({'x':[1], 'y':[2], 'c':['bad']}))


def test_lazy_collection_bounded_and_scale_before_sampling(tmp_path, monkeypatch):
    path = tmp_path / 'data.parquet'
    pl.DataFrame({'x':range(10_000), 'y':range(10_000), 'c':range(10_000)}).write_parquet(path)
    original = pl.LazyFrame.collect
    def bounded(lf, *args, **kwargs):
        result = original(lf, *args, **kwargs)
        assert result.height <= 20
        return result
    monkeypatch.setattr(pl.LazyFrame, 'collect', bounded)
    fig = heatmap('x', 'y', 'c', data=pl.scan_parquet(path).filter(pl.col('x') > 5000), max_points=20)
    assert fig.color_range == (5001, 9999)
    assert fig.color.max() < 9999  # unsampled maximum still defines the scale
    assert fig.replot((6000, 6100)).color_range == fig.color_range
