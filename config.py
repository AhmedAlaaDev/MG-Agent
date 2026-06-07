from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


def normalize_azure_openai_endpoint(endpoint: str) -> str:
    """
    AzureOpenAI SDK expects the resource root, e.g.
    https://houseblreader-resource.openai.azure.com/
    not the portal copy-paste path .../openai/v1
    """
    url = (endpoint or "").strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    return f"{url}/" if url else ""


class Settings(BaseSettings):
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azure_openai_api_version: str = Field(default="2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION")
    azure_openai_deployment: str = Field(default="gpt-4o", alias="AZURE_OPENAI_DEPLOYMENT")
    azure_openai_project_url: str = Field(
        default="",
        alias="AZURE_OPENAI_PROJECT_URL",
        description="Azure AI Foundry project URL (informational; SDK uses AZURE_OPENAI_ENDPOINT)",
    )

    @field_validator("azure_openai_endpoint", mode="before")
    @classmethod
    def _normalize_endpoint(cls, v: object) -> object:
        if isinstance(v, str) and v.strip():
            return normalize_azure_openai_endpoint(v)
        return v

    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    tesseract_lang: str = Field(default="eng", alias="TESSERACT_LANG")
    tesseract_cmd: str = Field(default=None, alias="TESSERACT_CMD")

    max_input_chars: int = Field(default=90000, alias="MAX_INPUT_CHARS")
    native_min_chars: int = Field(default=600, alias="NATIVE_MIN_CHARS")
    native_min_field_hits: int = Field(default=5, alias="NATIVE_MIN_FIELD_HITS")

    excel_max_rows_per_sheet: int = Field(default=2500, alias="EXCEL_MAX_ROWS_PER_SHEET")
    excel_max_cols_per_sheet: int = Field(default=80, alias="EXCEL_MAX_COLS_PER_SHEET")
    excel_max_cell_chars: int = Field(default=500, alias="EXCEL_MAX_CELL_CHARS")

    # LLM backend: "azure" (default) or "gemini"
    llm_provider: str = Field(default="azure", alias="LLM_PROVIDER")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    @property
    def uses_gemini(self) -> bool:
        return (self.llm_provider or "azure").strip().lower() == "gemini"

    @property
    def active_llm_model(self) -> str:
        return self.gemini_model if self.uses_gemini else self.azure_openai_deployment

    @property
    def llm_extraction_prefix(self) -> str:
        """Short tag used in extraction_method metadata (gemini vs azure)."""
        return "gemini" if self.uses_gemini else "azure"

    def llm_meta(self) -> dict[str, str]:
        return {
            "llm_provider": (self.llm_provider or "azure").strip().lower(),
            "llm_model": self.active_llm_model,
        }

    class Config:
        env_file = ".env"
        extra = "ignore"


Settings.model_rebuild()
settings = Settings()