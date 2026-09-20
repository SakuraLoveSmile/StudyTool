import os
import sys
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

    # Desktop Mode Settings
    DESKTOP_MODE: bool = False
    DESKTOP_INSTANCE_TOKEN: str = ""
    FRONTEND_DIST_DIR: Optional[str] = None
    
    # Logging & Diagnostics Settings
    LOG_LEVEL: str = "INFO"
    LOG_DIR: Optional[str] = None
    LOG_MAX_BYTES: int = 5242880  # 5 MiB per file (desktop / backend / frontend streams)
    LOG_BACKUP_COUNT: int = 5
    LOG_RETENTION_DAYS: int = 7
    
    # LLM Settings
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT: int = 60
    DEMO_MODE: bool = False
    
    # OCR Settings
    OCR_MODE: str = "vision_llm"  # "mock", "vision_llm", "local", "paddleocr_api"
    OCR_VISION_MODEL: str = "gpt-4o-mini"
    OCR_API_KEY: str = ""
    OCR_BASE_URL: str = ""
    BAIDU_OCR_API_URL: str = ""
    BAIDU_OCR_TOKEN: str = ""
    
    # Embedding Settings
    EMBEDDING_MODE: str = "mock"  # "mock", "remote"
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    model_config = SettingsConfigDict(
        env_file=None if (os.environ.get("STUDYTOOL_DESKTOP_MODE") or "--desktop" in sys.argv) else ".env",
        extra="allow",
    )

    def apply_desktop_payload(self, payload: dict) -> None:
        """Apply desktop configuration injected via stdin or bootstrap."""
        self.DESKTOP_MODE = True
        self.DEMO_MODE = False
        token = str(payload.get("instance_token", "")).strip()
        if not token:
            raise RuntimeError("Desktop instance token cannot be empty")
        self.DESKTOP_INSTANCE_TOKEN = token

        if payload.get("port"):
            self.PORT = int(payload["port"])
        if payload.get("host"):
            self.HOST = str(payload["host"])

        if payload.get("data_dir"):
            self.DATA_DIR = str(payload["data_dir"])

        # Models
        if "llm_base_url" in payload:
            self.LLM_BASE_URL = str(payload["llm_base_url"]).strip() or "https://api.openai.com/v1"
        if "llm_model" in payload:
            self.LLM_MODEL = str(payload["llm_model"]).strip() or "gpt-4o-mini"
        if "llm_api_key" in payload:
            self.LLM_API_KEY = str(payload["llm_api_key"]).strip()

        if "embedding_base_url" in payload:
            self.EMBEDDING_BASE_URL = str(payload["embedding_base_url"]).strip() or "https://api.openai.com/v1"
        if "embedding_model" in payload:
            self.EMBEDDING_MODEL = str(payload["embedding_model"]).strip() or "text-embedding-3-small"
        if "embedding_api_key" in payload:
            self.EMBEDDING_API_KEY = str(payload["embedding_api_key"]).strip()

        # Desktop strictly uses remote embedding (fails if key is missing, never falls back to mock)
        self.EMBEDDING_MODE = "remote"

        if "baidu_ocr_api_url" in payload:
            self.BAIDU_OCR_API_URL = str(payload["baidu_ocr_api_url"]).strip()
        if "baidu_ocr_token" in payload:
            self.BAIDU_OCR_TOKEN = str(payload["baidu_ocr_token"]).strip()

        # Strict OCR mode: paddleocr_api if baidu configured, else vision_llm
        if self.BAIDU_OCR_API_URL and self.BAIDU_OCR_TOKEN:
            self.OCR_MODE = "paddleocr_api"
        else:
            self.OCR_MODE = "vision_llm"

    @property
    def desktop_root_path(self) -> Path:
        custom = os.environ.get("STUDYTOOL_DESKTOP_DATA_DIR")
        if custom:
            p = Path(custom).resolve()
        elif self.DATA_DIR != "./data":
            p = Path(self.DATA_DIR).resolve()
        elif os.name == "nt" or "LOCALAPPDATA" in os.environ:
            p = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "StudyTool"
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "StudyTool"
        else:
            p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "StudyTool"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def root_data_path(self) -> Path:
        if self.DESKTOP_MODE:
            p = self.desktop_root_path / "data"
        else:
            p = Path(self.DATA_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def config_path(self) -> Path:
        if self.DESKTOP_MODE:
            p = self.desktop_root_path / "config"
        else:
            p = self.root_data_path / "config"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def backups_path(self) -> Path:
        if self.DESKTOP_MODE:
            p = self.desktop_root_path / "backups"
        else:
            p = self.root_data_path / "backups"
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
        elif self.DESKTOP_MODE:
            p = self.desktop_root_path / "logs"
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
