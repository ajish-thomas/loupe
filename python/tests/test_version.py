import loupe_kernel


def test_version_is_a_nonempty_string() -> None:
    assert isinstance(loupe_kernel.__version__, str)
    assert loupe_kernel.__version__
