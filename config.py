from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azure_openai_api_version: str = Field(default="2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION")
    azure_openai_deployment: str = Field(default="gpt-4o-mini", alias="AZURE_OPENAI_DEPLOYMENT")

    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    tesseract_lang: str = Field(default="eng", alias="TESSERACT_LANG")
    tesseract_cmd: str = Field(default=None, alias="TESSERACT_CMD")

    max_input_chars: int = Field(default=90000, alias="MAX_INPUT_CHARS")
    native_min_chars: int = Field(default=600, alias="NATIVE_MIN_CHARS")
    native_min_field_hits: int = Field(default=5, alias="NATIVE_MIN_FIELD_HITS")

    excel_max_rows_per_sheet: int = Field(default=2500, alias="EXCEL_MAX_ROWS_PER_SHEET")
    excel_max_cols_per_sheet: int = Field(default=80, alias="EXCEL_MAX_COLS_PER_SHEET")
    excel_max_cell_chars: int = Field(default=500, alias="EXCEL_MAX_CELL_CHARS")

    class Config:
        env_file = ".env"
        extra = "ignore"


Settings.model_rebuild()
settings = Settings()