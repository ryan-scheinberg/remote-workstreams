import pytest

from remote_workstreams.audio import pipeline as pipeline_module


@pytest.fixture(autouse=True)
def fast_endpoint_grace(monkeypatch):
    """The endpoint grace hold is 1.2s live; tests can't wait that per turn."""
    monkeypatch.setattr(pipeline_module, "_GRACE_S", 0.05)
