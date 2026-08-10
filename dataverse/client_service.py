"""
Singleton HTTP client for Microsoft Dynamics 365 Dataverse (OData v4).

Features:
  - OData v4 headers injected on every request
  - OAuth2 client-credentials (client_id / client_secret / tenant_id) token acquisition
  - Automatic Bearer token injection with refresh-on-expiry
  - Retry logic: network errors, 5xx, 429; no retry on 4xx (except 429)
  - Exponential backoff support
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_TOKEN_SCOPE = "{base_url}/.default"
_DEFAULT_RESOURCE_URL = "https://mgc.crm4.dynamics.com"


@dataclass
class RetryConfig:
    max_retries: int = 5
    delay: int = 2000
    use_exponential_backoff: bool = False


@dataclass
class CachedToken:
    access_token: str
    expires_at: float  # unix timestamp


class DataverseClientService:
    _instance: Optional["DataverseClientService"] = None

    base_url: str
    _session: requests.Session
    _retry_config: RetryConfig
    _cached_token: Optional[CachedToken] = None

    # --- env var keys (override via set_credentials if needed) ---
    _tenant_id: str = ""
    _client_id: str = ""
    _client_secret: str = ""

    def __new__(cls, retry_config: Optional[RetryConfig] = None) -> "DataverseClientService":
        required_state = ("_session", "base_url", "_resource_url")
        if cls._instance is None or not all(hasattr(cls._instance, key) for key in required_state):
            instance = super().__new__(cls)
            cls._instance = instance
            try:
                instance._init(retry_config or RetryConfig())
            except Exception:
                # Never cache a partially initialized singleton.  A later
                # serverless invocation must be able to retry cleanly after a
                # transient environment/configuration failure or warm reload.
                cls._instance = None
                raise
        return cls._instance

    @staticmethod
    def get_instance(retry_config: Optional[RetryConfig] = None) -> "DataverseClientService":
        required_state = ("_session", "base_url", "_resource_url")
        if (
            DataverseClientService._instance is None
            or not all(
                hasattr(DataverseClientService._instance, key)
                for key in required_state
            )
        ):
            DataverseClientService._instance = None
            DataverseClientService(retry_config or RetryConfig())
        return DataverseClientService._instance

    def _init(self, retry_config: RetryConfig) -> None:
        from dataverse.env_service import EnvService as _EnvService
        env = _EnvService.get_instance()

        resource_url = env.get_optional("BASE_URL", _DEFAULT_RESOURCE_URL).rstrip("/")
        configured_api_url = env.get_optional("AZURE_APP_API_URL", "").rstrip("/")
        self.base_url = configured_api_url or (
            f"{resource_url}/api/data/v9.2" if resource_url else ""
        )
        if not self.base_url:
            raise ValueError("AZURE_APP_API_URL or BASE_URL is not configured")

        self._tenant_id = env.get_optional("TENANT_ID", "")
        self._client_id = env.get_optional("CLIENT_ID", "")
        self._client_secret = env.get_optional("CLIENT_SECRET", "")
        # OAuth scope root: prefer explicit BASE_URL, else strip OData path from API URL
        self._resource_url = (
            resource_url
            or self.base_url.replace("/api/data/v9.2", "").rstrip("/")
        )

        if not all([self._tenant_id, self._client_id, self._client_secret]):
            raise ValueError(
                "TENANT_ID, CLIENT_ID, and CLIENT_SECRET must be set in .env"
            )

        self._retry_config = retry_config
        self._session = requests.Session()
        self._configure_session()

    def set_credentials(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._cached_token = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        return self._session

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("DELETE", url, **kwargs)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        now = time.time()
        if self._cached_token and now < self._cached_token.expires_at - 60:
            return self._cached_token.access_token

        token_url = _TOKEN_URL.format(tenant=self._tenant_id)
        scope = _TOKEN_SCOPE.format(base_url=self._resource_url)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": scope,
        }
        resp = requests.post(token_url, data=data, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to acquire Dataverse token: HTTP {resp.status_code} – {resp.text}"
            )
        body = resp.json()
        token = body["access_token"]
        expires_in = body.get("expires_in", 3600)
        self._cached_token = CachedToken(
            access_token=token,
            expires_at=now + float(expires_in),
        )
        return token

    # ------------------------------------------------------------------
    # Session configuration
    # ------------------------------------------------------------------

    def _configure_session(self) -> None:
        self._session.headers.update({
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
            "Prefer": 'odata.include-annotations="*"',
        })

    def _ensure_auth(self) -> None:
        token = self._get_access_token()
        self._session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Core request method with retry
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self._ensure_auth()
        full_url = url if url.startswith("http") else f"{self.base_url.rstrip('/')}/{url.lstrip('/')}"

        last_error: Optional[Exception] = None
        cfg = self._retry_config

        for attempt in range(cfg.max_retries + 1):
            try:
                resp = self._session.request(method, full_url, **kwargs)
                if resp.status_code < 400:
                    return resp
                if not self._should_retry(resp):
                    resp.raise_for_status()
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if not self._should_retry(getattr(exc, "response", None)):
                    raise

            if attempt < cfg.max_retries:
                backoff = (
                    cfg.delay * (2 ** attempt)
                    if cfg.use_exponential_backoff
                    else cfg.delay
                ) / 1000.0
                logger.info(
                    "[DataverseClient] Retrying %s %s (attempt %d/%d) after %.0fms",
                    method.upper(),
                    full_url,
                    attempt + 1,
                    cfg.max_retries,
                    backoff * 1000,
                )
                time.sleep(backoff)
                # Token may have expired mid-retry; refresh
                self._cached_token = None
                self._ensure_auth()

        raise last_error or RuntimeError(f"Request failed after {cfg.max_retries} retries")

    @staticmethod
    def _should_retry(response: Any) -> bool:
        if response is None:
            return True  # network error
        status = response.status_code if hasattr(response, "status_code") else None
        if status is None:
            return True
        if 500 <= status < 600:
            return True
        if status == 429:
            return True
        return False
