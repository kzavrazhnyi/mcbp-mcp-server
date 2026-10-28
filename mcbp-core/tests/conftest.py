from __future__ import annotations

import pytest

from mcbp_core.client import (
    reset_mock_chats,
    reset_mock_client_profiles,
    reset_mock_dashboard,
    reset_mock_function_status,
    reset_mock_functions,
)


@pytest.fixture(autouse=True)
def _clean_mock_chats():
    reset_mock_chats()
    reset_mock_dashboard()
    reset_mock_function_status()
    reset_mock_functions()
    reset_mock_client_profiles()
    yield
    reset_mock_chats()
    reset_mock_dashboard()
    reset_mock_function_status()
    reset_mock_functions()
    reset_mock_client_profiles()
