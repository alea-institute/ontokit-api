"""Storage service for MinIO object storage integration."""

import asyncio
from io import BytesIO
from typing import Any
from uuid import UUID

from minio import Minio
from minio.error import S3Error
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.core.database import async_session_maker
from ontokit.models.project import Project


class StorageError(Exception):
    """Exception raised for storage operation errors."""

    pass


def _read_and_release(response: Any) -> bytes:
    # urllib3's release_conn() can do socket I/O; combine with read() so the
    # whole response lifecycle stays off the event loop.
    try:
        data: bytes = response.read()
        return data
    finally:
        response.close()
        response.release_conn()


class StorageService:
    """Service for interacting with MinIO object storage."""

    def __init__(self) -> None:
        self.client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket = settings.minio_bucket

    async def ensure_bucket_exists(self) -> None:
        """Ensure the bucket exists, creating it if necessary."""
        try:
            # MinIO's Python client is synchronous (urllib3); run in a thread
            # so it never blocks the asyncio event loop.
            exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
            if not exists:
                await asyncio.to_thread(self.client.make_bucket, self.bucket)
        except S3Error as e:
            raise StorageError(f"Failed to ensure bucket exists: {e}") from e

    async def upload_file(self, object_name: str, data: bytes, content_type: str) -> str:
        """
        Upload a file to MinIO storage.

        Args:
            object_name: The object name/path in the bucket
            data: The file content as bytes
            content_type: The MIME type of the file

        Returns:
            The full path to the stored object

        Raises:
            StorageError: If the upload fails
        """
        try:
            await self.ensure_bucket_exists()
            await asyncio.to_thread(
                self.client.put_object,
                bucket_name=self.bucket,
                object_name=object_name,
                data=BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
            return f"{self.bucket}/{object_name}"
        except S3Error as e:
            raise StorageError(f"Failed to upload file: {e}") from e

    async def download_file(self, object_name: str) -> bytes:
        """
        Download a file from MinIO storage.

        Args:
            object_name: The object name/path in the bucket

        Returns:
            The file content as bytes

        Raises:
            StorageError: If the download fails
        """
        try:
            response = await asyncio.to_thread(
                self.client.get_object,
                bucket_name=self.bucket,
                object_name=object_name,
            )
            return await asyncio.to_thread(_read_and_release, response)
        except S3Error as e:
            raise StorageError(f"Failed to download file: {e}") from e

    async def delete_file(self, object_name: str) -> None:
        """
        Delete a file from MinIO storage.

        Args:
            object_name: The object name/path in the bucket

        Raises:
            StorageError: If the deletion fails
        """
        try:
            await asyncio.to_thread(
                self.client.remove_object,
                bucket_name=self.bucket,
                object_name=object_name,
            )
        except S3Error as e:
            raise StorageError(f"Failed to delete file: {e}") from e

    async def file_exists(self, object_name: str) -> bool:
        """
        Check if a file exists in MinIO storage.

        Args:
            object_name: The object name/path in the bucket

        Returns:
            True if the file exists, False otherwise
        """
        try:
            await asyncio.to_thread(
                self.client.stat_object,
                bucket_name=self.bucket,
                object_name=object_name,
            )
            return True
        except S3Error:
            return False

    async def delete_project_files(
        self, project_id: UUID, *, db: AsyncSession | None = None
    ) -> int:
        """Delete only this UUID's derived objects, preserving every referenced source.

        Source paths may be either object keys or bucket/key paths. References
        from *any* project protect an object, including references from live
        sources and other generations. The retention orchestrator only passes
        UUIDs and never uses a source path as a deletion target.
        """
        if not isinstance(project_id, UUID):
            raise TypeError("project_id must be a UUID")
        if db is None:
            async with async_session_maker() as session:
                return await self.delete_project_files(project_id, db=session)
        prefix = f"projects/{project_id}/"
        result = await db.execute(
            select(Project.source_file_path).where(
                or_(
                    Project.source_file_path.startswith(prefix, autoescape=True),
                    Project.source_file_path.startswith(f"{self.bucket}/{prefix}", autoescape=True),
                )
            )
        )
        protected = set()
        for path in result.scalars().all():
            if path is not None:
                protected.add(path)
                protected.add(path.removeprefix(f"{self.bucket}/"))

        def remove() -> int:
            count = 0
            try:
                # Iteration performs network I/O too, so keep the entire lazy
                # listing and deletion lifecycle off the event loop.
                for obj in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
                    key = obj.object_name
                    if key is not None and key.startswith(prefix) and key not in protected:
                        try:
                            self.client.remove_object(self.bucket, key)
                        except S3Error as exc:
                            if exc.code == "NoSuchKey":
                                continue
                            raise
                        count += 1
            except S3Error as exc:
                if exc.code == "NoSuchBucket":
                    return count
                raise StorageError("Project object deletion failed") from exc
            return count

        task = asyncio.create_task(asyncio.to_thread(remove))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the caller's generation lease until deletion actually stops.
            await task
            raise


def get_storage_service() -> StorageService:
    """Factory function for dependency injection."""
    return StorageService()
