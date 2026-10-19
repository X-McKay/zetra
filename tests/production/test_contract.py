"""SDK production contract: state authority belongs to Temporal, not a host process."""

import importlib.util


def test_sdk_exports_standard_worker_factory():
    assert importlib.util.find_spec("zetra.worker") is not None, "standard worker factory missing"
