import os
import secrets

import pytest


@pytest.fixture(autouse=True)
def scheduler_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = "test-scheduler-key"
    monkeypatch.setenv("SCHEDULER_API_KEY", key)
    return key
