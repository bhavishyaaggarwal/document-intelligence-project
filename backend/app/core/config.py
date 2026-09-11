from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    app_name: str = "Document Intelligence API"
    environment: str = "development"
    database_url: str = "sqlite:///./document_intelligence.db"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: int = 90
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_timeout_seconds: int = 120
    extraction_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:3b"
    ollama_timeout_seconds: int = 180
    max_upload_mb: int = 15
    max_pages: int = 3
    validation_tolerance: float = 0.01
    cors_origins: str = "*"

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()