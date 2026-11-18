from __future__ import annotations

import pytest

from agent_registry.store.memory import MemoryStore
from tests.store_suite import StoreSuite


class TestMemoryStore(StoreSuite):
    @pytest.fixture
    def store(self):
        return MemoryStore()
