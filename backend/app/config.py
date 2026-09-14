from typing import Optional
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DATA_DIR: str = "./data"
    PUBLIC_MODE: bool = False
    PUBLIC_ORIGIN: str = ""
    AUTH_COOKIE_SECURE: bool = True
    
    # Logging & Diagnostics Settings
    LOG_LEVEL: str = "INFO"
    LOG_DIR: Optional[str] = None
    LOG_MAX_BYTES: int = 10485760  # 10 MiB
    LOG_BACKUP_COUNT: int = 5
    
    # LLM Settings
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT: int = 60
    DEMO_MODE: bool = False
    
    # OCR Settings
    OCR_MODE: str = "vision_llm"  # "mock", "vision_llm", "local"
    OCR_VISION_MODEL: str = "gpt-4o-mini"
    OCR_API_KEY: str = ""
    OCR_BASE_URL: str = ""
    
    # Embedding Settings
    EMBEDDING_MODE: str = "mock"  # "mock", "remote"
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    @property
    def root_data_path(self) -> Path:
        p = Path(self.DATA_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_path(self) -> Path:
        from app.tenancy import current_tenant_id
        user_id = current_tenant_id()
        if user_id is None:
            if self.PUBLIC_MODE:
                raise RuntimeError("Tenant context required for business storage")
            return self.root_data_path
        root = self.root_data_path / "users"
        p = root / user_id
        # Symlinks are not a supported tenant storage mechanism.
        if root.is_symlink() or p.is_symlink() or p.resolve().parent != root.resolve():
            raise RuntimeError("Unsafe tenant storage path")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def uploads_path(self) -> Path:
        p = self.data_path / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_path(self) -> Path:
        if self.LOG_DIR:
            p = Path(self.LOG_DIR).resolve()
        else:
            p = self.root_data_path / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "app.db"

    def get_masked_api_key(self) -> str:
        if not self.LLM_API_KEY:
            return ""
        if len(self.LLM_API_KEY) <= 8:
            return "***"
        return f"{self.LLM_API_KEY[:4]}***{self.LLM_API_KEY[-4:]}"

settings = Settings()
