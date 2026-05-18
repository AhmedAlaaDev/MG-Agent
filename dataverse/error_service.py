"""Singleton service for standardized error handling across the application.

Handles various error types including Axios-like errors (from requests),
OData/Dataverse error responses, and general exceptions, transforming them
into standardized error messages.
"""

import json
import logging
from typing import Any, Callable, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DataverseErrorService:
    _instance: "DataverseErrorService | None" = None

    def __new__(cls) -> "DataverseErrorService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def get_instance() -> "DataverseErrorService":
        if DataverseErrorService._instance is None:
            DataverseErrorService()
        return DataverseErrorService._instance

    async def handle(self, fn: Callable[[], T]) -> T:
        try:
            return await fn()
        except Exception as error:
            logger.error("Error: %s", error)
            raise self._handle_error(error)

    def _handle_error(self, error: Exception) -> Exception:
        if isinstance(error, requests.exceptions.ConnectionError):
            return Exception(
                "Network error: connection was reset by the server. Please retry."
            )
        if isinstance(error, requests.exceptions.Timeout):
            return Exception(
                "Network timeout while contacting the server. Please retry."
            )
        if isinstance(error, requests.exceptions.RequestException):
            return self._handle_requests_error(error)
        if isinstance(error, Exception):
            return error
        if isinstance(error, str):
            return Exception(self._extract_message_from_text(error))
        if hasattr(error, "message") and isinstance(error.message, str):
            return Exception(error.message)
        return Exception("Unknown error occurred.")

    def _handle_requests_error(self, error: requests.exceptions.RequestException) -> Exception:
        response = error.response
        if response is None:
            return Exception(str(error))
        status = response.status_code
        if status == 401:
            raise Exception("Unauthorized. No token provided or invalid token.")
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            data = response.text
        if isinstance(data, dict):
            msg = (
                data.get("error", {}).get("message")
                or data.get("message")
                or str(data)
            )
            return Exception(f"Error {status}: {msg}")
        if isinstance(data, str):
            return Exception(self._extract_message_from_text(data))
        return Exception(f"HTTP {status}: {error}")

    def _extract_message_from_text(self, text: str) -> str:
        import re
        msg_match = re.search(r'"(?:[Mm]essage)"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if msg_match:
            return f"Error: {self._unescape_json_string(msg_match.group(1))}"
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                msg = parsed.get("error", {}).get("message", "Unknown error")
                return f"Error: {msg}"
        except (ValueError, json.JSONDecodeError):
            pass
        return "Unknown error message format."

    @staticmethod
    def _unescape_json_string(s: str) -> str:
        return s.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
