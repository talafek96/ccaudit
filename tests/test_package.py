"""Contract on the package skeleton: the distribution is importable and versioned."""

import ccaudit


def test_package_exposes_a_version() -> None:
    assert isinstance(ccaudit.__version__, str)
    assert ccaudit.__version__
