from __future__ import annotations

import pytest

from mcbp_core.client import reset_mock_chats


@pytest.fixture(autouse=True)
def _clean_mock_chats():
    reset_mock_chats()
    yield
    reset_mock_chats()
