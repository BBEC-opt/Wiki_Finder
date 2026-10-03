import re
import shutil
from pathlib import Path

from app.core.errors import AppError


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r"[^\w.()\-\u4e00-\u9fff ]", "_", name)
    if not name or name in {".", ".."}:
        raise AppError("invalid_filename", "文件名无效")
    return name[:200]


class LocalStorage:
    def __init__(self, upload_root: Path, artifact_root: Path):
        self.upload_root = upload_root.resolve()
        self.artifact_root = artifact_root.resolve()

    def knowledge_dir(self, kb_id: str, knowledge_id: str) -> Path:
        return self._inside(self.upload_root, self.upload_root / kb_id / knowledge_id)

    def artifact_dir(self, knowledge_id: str) -> Path:
        return self._inside(self.artifact_root, self.artifact_root / knowledge_id)

    def knowledge_base_dir(self, kb_id: str) -> Path:
        return self._inside(self.upload_root, self.upload_root / kb_id)

    def remove_knowledge(self, kb_id: str, knowledge_id: str) -> None:
        for path in (self.knowledge_dir(kb_id, knowledge_id), self.artifact_dir(knowledge_id)):
            if path.exists():
                shutil.rmtree(path)

    def remove_knowledge_base(self, kb_id: str, knowledge_ids: list[str]) -> None:
        upload_path = self.knowledge_base_dir(kb_id)
        if upload_path.exists():
            shutil.rmtree(upload_path)
        for knowledge_id in knowledge_ids:
            artifact_path = self.artifact_dir(knowledge_id)
            if artifact_path.exists():
                shutil.rmtree(artifact_path)

    async def initialize(self) -> None:
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        pass

    async def persist_source(self, kb_id: str, knowledge_id: str, path: Path) -> None:
        """本地模式已经写入最终位置，无需额外同步。"""

    async def ensure_source(self, kb_id: str, knowledge_id: str, file_name: str) -> Path:
        return self.knowledge_dir(kb_id, knowledge_id) / safe_filename(file_name)

    async def persist_artifacts(self, knowledge_id: str) -> None:
        """本地模式的解析器直接写入产物目录。"""

    async def ensure_artifacts(self, knowledge_id: str) -> None:
        """本地产物始终直接可读。"""

    async def delete_knowledge(self, kb_id: str, knowledge_id: str) -> None:
        self.remove_knowledge(kb_id, knowledge_id)

    async def delete_knowledge_base(self, kb_id: str, knowledge_ids: list[str]) -> None:
        self.remove_knowledge_base(kb_id, knowledge_ids)

    @staticmethod
    def _inside(root: Path, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise AppError("unsafe_path", "非法文件路径")
        return resolved


class MinioStorage(LocalStorage):
    """以本地目录为解析缓存、MinIO 为持久层的对象存储实现。"""

    def __init__(self, upload_root: Path, artifact_root: Path, endpoint: str, access_key: str,
                 secret_key: str, bucket: str, secure: bool = False, region: str = ""):
        super().__init__(upload_root, artifact_root)
        self.endpoint = endpoint.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self.region = region or None
        self.client = None

    async def initialize(self) -> None:
        await super().initialize()
        try:
            from miniopy_async import Minio
        except ImportError as exc:
            raise RuntimeError('MinIO 模式需要安装项目的 "production" 可选依赖') from exc
        self.client = Minio(
            self.endpoint, access_key=self.access_key, secret_key=self.secret_key,
            secure=self.secure, region=self.region,
        )
        if not await self.client.bucket_exists(self.bucket):
            await self.client.make_bucket(self.bucket, location=self.region)

    async def persist_source(self, kb_id: str, knowledge_id: str, path: Path) -> None:
        self._require_client()
        key = f"source/{kb_id}/{knowledge_id}/{safe_filename(path.name)}"
        await self.client.fput_object(self.bucket, key, str(path))

    async def ensure_source(self, kb_id: str, knowledge_id: str, file_name: str) -> Path:
        self._require_client()
        target = self.knowledge_dir(kb_id, knowledge_id) / safe_filename(file_name)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            key = f"source/{kb_id}/{knowledge_id}/{safe_filename(file_name)}"
            await self.client.fget_object(self.bucket, key, str(target))
        return target

    async def persist_artifacts(self, knowledge_id: str) -> None:
        self._require_client()
        root = self.artifact_dir(knowledge_id)
        if not root.exists():
            return
        for path in root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                await self.client.fput_object(self.bucket, f"artifact/{knowledge_id}/{relative}", str(path))

    async def ensure_artifacts(self, knowledge_id: str) -> None:
        self._require_client()
        root = self.artifact_dir(knowledge_id)
        manifest = root / "manifest.json"
        if manifest.exists():
            return
        root.mkdir(parents=True, exist_ok=True)
        async for item in self.client.list_objects(self.bucket, prefix=f"artifact/{knowledge_id}/", recursive=True):
            relative = item.object_name.removeprefix(f"artifact/{knowledge_id}/")
            if not relative:
                continue
            target = self._inside(root, root / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            await self.client.fget_object(self.bucket, item.object_name, str(target))

    async def delete_knowledge(self, kb_id: str, knowledge_id: str) -> None:
        self._require_client()
        await self._delete_prefix(f"source/{kb_id}/{knowledge_id}/")
        await self._delete_prefix(f"artifact/{knowledge_id}/")
        self.remove_knowledge(kb_id, knowledge_id)

    async def delete_knowledge_base(self, kb_id: str, knowledge_ids: list[str]) -> None:
        self._require_client()
        await self._delete_prefix(f"source/{kb_id}/")
        for knowledge_id in knowledge_ids:
            await self._delete_prefix(f"artifact/{knowledge_id}/")
        self.remove_knowledge_base(kb_id, knowledge_ids)

    async def _delete_prefix(self, prefix: str) -> None:
        async for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            await self.client.remove_object(self.bucket, item.object_name)

    def _require_client(self) -> None:
        if self.client is None:
            raise RuntimeError("MinIO storage is not initialized")


def create_storage(settings):
    if settings.object_storage_driver == "minio":
        return MinioStorage(
            settings.upload_dir, settings.artifact_dir, settings.minio_endpoint,
            settings.minio_access_key, settings.minio_secret_key, settings.minio_bucket,
            settings.minio_secure, settings.minio_region,
        )
    return LocalStorage(settings.upload_dir, settings.artifact_dir)
