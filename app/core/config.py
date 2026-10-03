"""应用配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: Path = Path("./data/demo.db")
    database_url: str = ""
    database_pool_min_size: int = Field(1, ge=1, le=50)
    database_pool_max_size: int = Field(10, ge=1, le=100)
    upload_dir: Path = Path("./data/uploads")
    artifact_dir: Path = Path("./data/artifacts")
    temp_dir: Path = Path("./data/tmp")
    object_storage_driver: str = "local"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "my-wiki"
    minio_secure: bool = False
    minio_region: str = ""

    vector_index_driver: str = "memory"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection_prefix: str = "my_wiki"
    qdrant_timeout_seconds: int = Field(10, ge=1, le=120)

    task_notifier_driver: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "my_wiki:dev"
    run_embedded_worker: bool = True
    max_upload_mb: int = Field(50, ge=1, le=500)
    cors_origins: str = ""

    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: str = ""
    chat_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = Field(256, ge=8)
    model_timeout_seconds: int = Field(60, ge=1)

    chunk_size: int = Field(800, ge=100)
    chunk_overlap: int = Field(100, ge=0)
    parent_chunk_size: int = Field(3_200, ge=200)
    embedding_batch_size: int = Field(10, ge=1, le=256)
    default_top_k: int = Field(5, ge=1, le=20)
    max_context_chars: int = Field(12_000, ge=1_000)
    worker_max_attempts: int = Field(3, ge=1, le=10)
    task_lease_seconds: int = Field(300, ge=30)
    wiki_extraction_granularity: str = "standard"
    wiki_max_candidates: int = Field(12, ge=3, le=30)

    ocr_engine: str = "none"

    @model_validator(mode="after")
    def validate_settings(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.parent_chunk_size < self.chunk_size:
            raise ValueError("PARENT_CHUNK_SIZE must be greater than or equal to CHUNK_SIZE")
        if self.wiki_extraction_granularity not in {"focused", "standard", "exhaustive"}:
            raise ValueError("WIKI_EXTRACTION_GRANULARITY must be focused, standard or exhaustive")
        if self.database_url and not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL currently supports PostgreSQL URLs only")
        if self.database_pool_min_size > self.database_pool_max_size:
            raise ValueError("DATABASE_POOL_MIN_SIZE cannot exceed DATABASE_POOL_MAX_SIZE")
        if self.object_storage_driver not in {"local", "minio"}:
            raise ValueError("OBJECT_STORAGE_DRIVER must be local or minio")
        if self.vector_index_driver not in {"memory", "qdrant"}:
            raise ValueError("VECTOR_INDEX_DRIVER must be memory or qdrant")
        if self.task_notifier_driver not in {"memory", "redis"}:
            raise ValueError("TASK_NOTIFIER_DRIVER must be memory or redis")
        if not self.run_embedded_worker and self.task_notifier_driver != "redis":
            raise ValueError("RUN_EMBEDDED_WORKER=false requires TASK_NOTIFIER_DRIVER=redis")
        if self.object_storage_driver == "minio" and not (self.minio_access_key and self.minio_secret_key):
            raise ValueError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required for MinIO")
        return self

    @property
    def provider_mode(self) -> str:
        """有 API Key 就使用真实模型，否则自动使用离线实现。"""
        return "openai" if self.model_api_key else "offline"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def database_driver(self) -> str:
        return "postgresql" if self.database_url else "sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
