"""Request-scoped LLM provider / model overrides for extraction endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from config import settings, is_valid_gemini_model

_llm_provider: ContextVar[Optional[str]] = ContextVar("llm_provider", default=None)
_llm_model: ContextVar[Optional[str]] = ContextVar("llm_model", default=None)


def normalize_llm_provider(provider: Optional[str]) -> str:
    normalized = (provider or settings.llm_provider or "azure").strip().lower()
    if normalized in ("puter", "puterjs", "puter.js"):
        return "puter"
    if normalized in ("gemini", "google"):
        return "gemini"
    return "azure"


def effective_llm_provider() -> str:
    override = _llm_provider.get()
    if override:
        return normalize_llm_provider(override)
    return normalize_llm_provider(settings.llm_provider)


def effective_llm_model() -> str:
    override = _llm_model.get()
    if override and override.strip():
        return override.strip()
    if effective_llm_provider() == "puter":
        return settings.puter_model
    if effective_llm_provider() == "gemini":
        return settings.gemini_model
    return settings.azure_openai_deployment


def uses_gemini() -> bool:
    return effective_llm_provider() == "gemini"


def uses_puter() -> bool:
    return effective_llm_provider() == "puter"


def llm_meta() -> dict[str, str]:
    provider = effective_llm_provider()
    return {
        "llm_provider": provider,
        "llm_model": effective_llm_model(),
    }


def llm_extraction_prefix() -> str:
    if uses_puter():
        return "puter"
    return "gemini" if uses_gemini() else "azure"


def validate_llm_request(provider: Optional[str], model: Optional[str]) -> None:
    """Raise ValueError when LLM provider/model combination is invalid."""
    effective = normalize_llm_provider(provider) if provider else effective_llm_provider()
    if model:
        model = model.strip()
        if model.startswith("gemini-") and effective not in ("gemini", "puter"):
            raise ValueError(
                f"Model '{model}' is a Gemini model. Set llm_provider=puter or gemini to use it."
            )
        if effective in ("gemini", "puter") and not is_valid_gemini_model(model):
            from config import GEMINI_MODELS

            raise ValueError(
                f"Invalid Gemini model '{model}'. "
                f"Choose one of: {', '.join(GEMINI_MODELS)}"
            )


@contextmanager
def llm_request_overrides(
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Iterator[None]:
    """Temporarily override LLM_PROVIDER / GEMINI_MODEL (or Azure deployment) for one request."""
    t_provider = _llm_provider.set(provider.strip().lower()) if provider else None
    t_model = _llm_model.set(model.strip()) if model else None
    try:
        yield
    finally:
        if t_provider is not None:
            _llm_provider.reset(t_provider)
        if t_model is not None:
            _llm_model.reset(t_model)
