"""Contract on the package skeleton: the distribution is importable and versioned."""

import claude_cost_tracker


def test_package_exposes_a_version() -> None:
    assert isinstance(claude_cost_tracker.__version__, str)
    assert claude_cost_tracker.__version__
