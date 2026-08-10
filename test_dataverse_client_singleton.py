from unittest.mock import patch

import pytest

from dataverse.client_service import DataverseClientService


def test_failed_initialization_does_not_cache_partial_singleton() -> None:
    previous = DataverseClientService._instance
    DataverseClientService._instance = None
    try:
        with patch.object(DataverseClientService, "_init", side_effect=RuntimeError("temporary")):
            with pytest.raises(RuntimeError, match="temporary"):
                DataverseClientService()
        assert DataverseClientService._instance is None
    finally:
        DataverseClientService._instance = previous


def test_get_instance_rebuilds_partial_singleton() -> None:
    previous = DataverseClientService._instance
    partial = object.__new__(DataverseClientService)
    DataverseClientService._instance = partial
    try:
        with patch.object(DataverseClientService, "_init") as initialize:
            rebuilt = DataverseClientService.get_instance()
        assert rebuilt is not partial
        initialize.assert_called_once()
    finally:
        DataverseClientService._instance = previous
