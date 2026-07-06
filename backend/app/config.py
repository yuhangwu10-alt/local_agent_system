from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/dbname"

    # OCR Provider
    ocr_provider: str = "qwen_vl"
    ocr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ocr_api_key: str = ""
    ocr_model: str = "qwen3-vl-flash"
    ocr_request_timeout: float = 180.0
    ocr_request_retries: int = 3
    ocr_retry_delay_seconds: float = 2.0
    ocr_fail_threshold: float = 0.8
    ocr_pdf_render_scale: float = 2.0

    # LLM Provider
    llm_provider: str = "qwen"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"

    # Storage
    storage_path: str = "/app/storage"

    # Page pool scoring thresholds (0-100 分制)
    score_core_threshold: float = 70
    score_borderline_threshold: float = 40

    model_config = {"env_file": (".env", "../.env"), "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
