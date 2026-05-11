from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import tos

from .config import TosConfig
from .naming import caesar_encrypt_filename
from .runner import BackupArtifact

LOGGER = logging.getLogger(__name__)
SINGLE_PUT_LIMIT = 5 * 1024 * 1024 * 1024
MULTIPART_PART_SIZE = 128 * 1024 * 1024
MULTIPART_TASK_NUM = 4


class TosUploader:
    def __init__(self, config: TosConfig) -> None:
        self.config = config
        if not config.access_key_id or not config.secret_access_key:
            raise ValueError(
                f"missing TOS credentials in {config.access_key_id_env}/"
                f"{config.secret_access_key_env}"
            )
        self.client = tos.TosClientV2(
            config.access_key_id,
            config.secret_access_key,
            config.endpoint,
            config.region,
        )

    def upload_artifact(self, artifact: BackupArtifact, dry_run: bool = False) -> str:
        key = self.object_key(artifact.path)
        if dry_run:
            LOGGER.info("would upload %s to tos://%s/%s", artifact.path, self.config.bucket, key)
            return key

        kwargs = {}
        if self.config.storage_class:
            kwargs["storage_class"] = _storage_class(self.config.storage_class)

        size = artifact.path.stat().st_size
        LOGGER.info("uploading %s to tos://%s/%s", artifact.path, self.config.bucket, key)
        if size >= SINGLE_PUT_LIMIT:
            checkpoint = f"{artifact.path}.tos-checkpoint"
            self.client.upload_file(
                self.config.bucket,
                key,
                str(artifact.path),
                part_size=MULTIPART_PART_SIZE,
                task_num=MULTIPART_TASK_NUM,
                enable_checkpoint=True,
                checkpoint_file=checkpoint,
                **kwargs,
            )
            if os.path.exists(checkpoint):
                os.remove(checkpoint)
        else:
            self.client.put_object_from_file(
                self.config.bucket,
                key,
                str(artifact.path),
                **kwargs,
            )
        return key

    def object_key(self, path: Path) -> str:
        prefix = self.config.prefix.format(hostname=socket.gethostname()).strip("/")
        remote_name = caesar_encrypt_filename(path.name)
        return f"{prefix}/{remote_name}" if prefix else remote_name

    def delete_key(self, key: str, dry_run: bool = False) -> None:
        if dry_run:
            LOGGER.info("would delete tos://%s/%s", self.config.bucket, key)
            return
        LOGGER.info("deleting tos://%s/%s", self.config.bucket, key)
        self.client.delete_object(self.config.bucket, key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        is_truncated = True
        marker = ""
        while is_truncated:
            output = self.client.list_objects(self.config.bucket, prefix=prefix, marker=marker)
            for obj in output.contents:
                keys.append(obj.key)
            is_truncated = bool(output.is_truncated)
            marker = output.next_marker
        return sorted(keys)


def _storage_class(value: str):
    normalized = value.strip().lower().replace("-", "_")
    mapping = {
        "standard": tos.StorageClassType.Storage_Class_Standard,
        "ia": tos.StorageClassType.Storage_Class_Ia,
        "archive": tos.StorageClassType.Storage_Class_Archive,
        "cold_archive": tos.StorageClassType.Storage_Class_Cold_Archive,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(mapping))
        raise ValueError(f"unsupported tos.storage_class {value!r}; allowed: {allowed}") from exc
