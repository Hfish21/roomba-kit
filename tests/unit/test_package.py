import roomba_kit


def test_version_is_exposed() -> None:
    assert roomba_kit.__version__ == "0.1.0"
