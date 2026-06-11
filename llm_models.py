"""OpenAPI enums for LLM provider and Gemini model selection."""

from enum import Enum

from config import GEMINI_MODELS


class LlmProviderQuery(str, Enum):
    azure = "azure"
    gemini = "gemini"


def _gemini_enum_member_name(model_id: str) -> str:
    return model_id.replace("-", "_").replace(".", "_")


GeminiModelQuery = Enum(
    "GeminiModelQuery",
    {_gemini_enum_member_name(m): m for m in GEMINI_MODELS},
    type=str,
)
