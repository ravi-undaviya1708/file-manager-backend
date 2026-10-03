"""Application configuration loaded from environment variables."""

from __future__ import annotations

from typing import List, Optional
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
    
    # Cashfree Payments Configuration
    CASHFREE_APP_ID: str = ""
    CASHFREE_SECRET_KEY: str = ""
    CASHFREE_WEBHOOK_SECRET: str = ""
    CASHFREE_ENV: str = "SANDBOX"
    CASHFREE_MODE: str = ""
    CASHFREE_API_VERSION: str = "2025-01-01"
    CASHFREE_SUBSCRIPTION_SERVICE: str = "pg"  # "pg" for PG Subscriptions v2, "sub" for Subscriptions v1
    CASHFREE_WEBHOOK_MAX_SKEW_SECONDS: int = 300  # 5 minutes maximum webhook timestamp skew

    # Background Billing Expiry Scheduler Configuration
    BILLING_EXPIRY_WORKER_ENABLED: bool = True
    BILLING_EXPIRY_INTERVAL_SECONDS: int = 21600  # Run every 6 hours by default
    
    # Backblaze B2 Storage Configuration
    B2_KEY_ID: str = ""
    B2_APPLICATION_KEY: str = ""
    B2_BUCKET: str = ""
    B2_ENDPOINT: str = ""

    # Contact & Support Email Configuration
    CONTACT_EMAIL: str = "undaviyaraj2000@gmail.com"

    # SMTP Server & Admin Notification Configuration
    ADMIN_NOTIFICATION_EMAIL: Optional[str] = None
    ADMIN_NOTIFICATIONS_ENABLED: bool = True
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 465
    SMTP_USERNAME: Optional[str] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: str = "GetFileNova"

    @property
    def effective_smtp_user(self) -> Optional[str]:
        """Return configured SMTP username with fallback to legacy SMTP_USER."""
        return self.SMTP_USERNAME or self.SMTP_USER

    @property
    def effective_from_email(self) -> Optional[str]:
        """Return configured From email address with fallback to SMTP username."""
        return self.SMTP_FROM_EMAIL or self.effective_smtp_user

    # Google Analytics 4 (GA4) Server-Side Data API Configuration
    GA4_PROPERTY_ID: Optional[str] = None
    GOOGLE_SERVICE_ACCOUNT_EMAIL: Optional[str] = None
    GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY: Optional[str] = None
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = None

    @property
    def formatted_google_private_key(self) -> Optional[str]:
        """Normalize multiline private key string for Vercel/cloud environments (converts literal \\n to newlines)."""
        if not self.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY:
            return None
        key = self.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY.strip()
        # Remove optional surrounding quotes if present
        if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
            key = key[1:-1]
        return key.replace("\\n", "\n")

    @property
    def is_ga4_configured(self) -> bool:
        """Check if minimum required credentials for GA4 Data API are present."""
        has_direct_creds = bool(self.GA4_PROPERTY_ID and self.GOOGLE_SERVICE_ACCOUNT_EMAIL and self.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY)
        has_file_creds = bool(self.GA4_PROPERTY_ID and self.GOOGLE_APPLICATION_CREDENTIALS)
        return has_direct_creds or has_file_creds

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
