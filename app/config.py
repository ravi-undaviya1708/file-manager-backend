"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import List
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "file_manager"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"
    APP_ENV: str = "development"
    JWT_SECRET_KEY: str = "supersecretkeyforlocaldevelopmentfilestoreapp"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days in minutes
    GOOGLE_CLIENT_ID: str = ""
    
    # Razorpay Payments Configuration
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    
    # Backblaze B2 Storage Configuration
    B2_KEY_ID: str = ""
    B2_APPLICATION_KEY: str = ""
    B2_BUCKET: str = ""
    B2_ENDPOINT: str = ""

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
