"""Smoke test: package imports cleanly."""

import sec_financials


def test_package_has_version():
    assert sec_financials.__version__ == "0.1.0"
