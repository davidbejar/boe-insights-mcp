import boe_insights_mcp


def test_package_exposes_non_empty_version() -> None:
    assert isinstance(boe_insights_mcp.__version__, str)
    assert boe_insights_mcp.__version__
