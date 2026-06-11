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

    # Gemini has a far larger context window than Azure GPT-4o, so it can read
    # long manifests/workbooks in one shot. Text beyond this budget is chunked
    # (page/line windows) and merged instead of being silently truncated.
    gemini_max_input_chars: int = Field(default=900000, alias="GEMINI_MAX_INPUT_CHARS")
    # When a PDF is uploaded with provider=gemini, send the original PDF bytes to
    # the model (true layout/table understanding) alongside the OCR text.
    gemini_native_pdf: bool = Field(default=True, alias="GEMINI_NATIVE_PDF")
    # When an Excel/CSV workbook is uploaded with provider=gemini, send the original
    # file bytes so the model reads sheet layout, merged cells, and column headers.
    gemini_native_spreadsheet: bool = Field(default=True, alias="GEMINI_NATIVE_SPREADSHEET")
    # Inline PDFs up to this size; larger PDFs go through the Files API.
    gemini_inline_pdf_max_bytes: int = Field(
        default=18_000_000, alias="GEMINI_INLINE_PDF_MAX_BYTES"
    )
    # Minimum spreadsheet rows before preferring one whole-workbook Gemini call
    # over per-row LLM calls (much better for long manifests).
    gemini_workbook_llm_min_rows: int = Field(default=3, alias="GEMINI_WORKBOOK_LLM_MIN_ROWS")

    excel_max_rows_per_sheet: int = Field(default=2500, alias="EXCEL_MAX_ROWS_PER_SHEET")
    excel_max_cols_per_sheet: int = Field(default=80, alias="EXCEL_MAX_COLS_PER_SHEET")
    excel_max_cell_chars: int = Field(default=500, alias="EXCEL_MAX_CELL_CHARS")

    # LLM backend: "azure" (default) or "gemini"
    llm_provider: str = Field(default="azure", alias="LLM_PROVIDER")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # Post-extraction CRM business rules (freight→booking, load type, TEUs, totals).
    custom_business_rules_enabled: bool = Field(
        default=True, alias="CUSTOM_BUSINESS_RULES_ENABLED"
    )

    class Config:
        env_file = ".env"
        extra = "ignore"


# Gemini models supported via Google AI API (same ids as Puter.js free tier).
GEMINI_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.5-flash-preview-09-2025",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro-preview",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


Settings.model_rebuild()
settings = Settings()


def is_valid_gemini_model(model: str) -> bool:
    return (model or "").strip() in GEMINI_MODELS