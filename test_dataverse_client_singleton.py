from unittest.mock import patch

import pytest

from dataverse.client_service import DataverseClientService


class _FakeEnv:
    values = {
        "BASE_URL": "https://example.crm.dynamics.com",
        "TENANT_ID": "tenant",
        "CLIENT_ID": "client",
        "CLIENT_SECRET": "secret",
    }

    def get_optional(self, key: str, default: str) -> str:
        return self.values.get(key, default)


class _FakeEnvWithoutBase(_FakeEnv):
    values = {
        "TENANT_ID": "tenant",
        "CLIENT_ID": "client",
        "CLIENT_SECRET": "secret",
    }


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


def test_base_url_builds_dataverse_api_url_when_explicit_api_url_is_missing() -> None:
    previous = DataverseClientService._instance
    DataverseClientService._instance = None
    try:
        with patch("dataverse.env_service.EnvService.get_instance", return_value=_FakeEnv()):
            client = DataverseClientService()
        assert client.base_url == "https://example.crm.dynamics.com/api/data/v9.2"
        assert client._resource_url == "https://example.crm.dynamics.com"
    finally:
        DataverseClientService._instance = previous


def test_known_crm_url_is_used_when_serverless_base_url_is_missing() -> None:
    previous = DataverseClientService._instance
    DataverseClientService._instance = None
    try:
        with patch("dataverse.env_service.EnvService.get_instance", return_value=_FakeEnvWithoutBase()):
            client = DataverseClientService()
        assert client.base_url == "https://mgc.crm4.dynamics.com/api/data/v9.2"
        assert client._resource_url == "https://mgc.crm4.dynamics.com"
    finally:
        DataverseClientService._instance = previous
