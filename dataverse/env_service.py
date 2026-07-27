"""Singleton service for accessing environment variables with type conversion."""

import os
from typing import Any, List, Optional, TypeVar

from dotenv import load_dotenv

load_dotenv()

T = TypeVar("T", str, int, bool)


class EnvService:
    _instance: "Optional[EnvService]" = None

    def __new__(cls) -> "EnvService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def get_instance() -> "EnvService":
        if EnvService._instance is None:
            EnvService()
        return EnvService._instance

    def get(self, key: str, expected_type: type = str) -> Any:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            raise ValueError(
                f'Missing environment variable "{key}".\n'
                f"Please ensure it is defined in your .env file."
            )
        return self._cast(raw, expected_type, key)

    def get_all(self, keys: List[str]) -> List[str]:
        return [self.get(k) for k in keys]

    def get_optional(self, key: str, default: T) -> T:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return self._cast(raw, type(default), key)
        except (ValueError, TypeError):
            return default

    def _cast(self, raw: str, target_type: type, key: str) -> Any:
        if target_type is int:
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f'"{key}" must be a valid number. Found: "{raw}"')
        if target_type is bool:
            if raw.lower() in ("true", "1"):
                return True
            if raw.lower() in ("false", "0"):
                return False
            raise ValueError(
                f'Environment variable "{key}" must be "true", "false", "1", or "0". Found: "{raw}"'
            )
        return raw

    @classmethod
    def reload(cls) -> None:
        """Reload environment variables from .env file(s)."""
        from dotenv import load_dotenv
        load_dotenv()
        cls._instance = None
