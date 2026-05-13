from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import time
from pathlib import Path

import tos

from .config import TosConfig
from .naming import caesar_encrypt_filename
from .runner import BackupArtifact

LOGGER = logging.getLogger(__name__)
SINGLE_PUT_LIMIT = 5 * 1024 * 1024 * 1024
MULTIPART_PART_SIZE = 128 * 1024 * 1024
MULTIPART_TASK_NUM = 16


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
        progress = _UploadProgress(size, artifact.path.name)
        LOGGER.info("uploading %s to tos://%s/%s", artifact.path, self.config.bucket, key)
        if size >= SINGLE_PUT_LIMIT:
            checkpoint = f"{artifact.path}.tos-checkpoint"
            try:
                self.client.upload_file(
                    self.config.bucket,
                    key,
                    str(artifact.path),
                    part_size=MULTIPART_PART_SIZE,
                    task_num=MULTIPART_TASK_NUM,
                    enable_checkpoint=True,
                    checkpoint_file=checkpoint,
                    data_transfer_listener=progress,
                    **kwargs,
                )
            finally:
                progress.finish()
            if os.path.exists(checkpoint):
                os.remove(checkpoint)
        else:
            try:
                self.client.put_object_from_file(
                    self.config.bucket,
                    key,
                    str(artifact.path),
                    data_transfer_listener=progress,
                    **kwargs,
                )
            finally:
                progress.finish()
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


class _UploadProgress:
    def __init__(self, total_bytes: int, label: str) -> None:
        self.total_bytes = max(total_bytes, 1)
        self.label = label
        self.started_at = time.monotonic()
        self.last_render_at = 0.0
        self.last_logged_percent = -1
        self.is_tty = sys.stderr.isatty()
        self.finished = False

    def __call__(self, consumed_bytes: int, total_bytes: int, rw_once_bytes: int, transfer_type) -> None:
        self.total_bytes = max(total_bytes or self.total_bytes, 1)
        status = getattr(transfer_type, "name", str(transfer_type))
        if status.endswith("Failed"):
            self._render(consumed_bytes, failed=True, force=True)
            return
        self._render(consumed_bytes, force=status.endswith("Succeed"))

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self.is_tty:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _render(self, consumed_bytes: int, failed: bool = False, force: bool = False) -> None:
        now = time.monotonic()
        percent = min(100.0, consumed_bytes * 100.0 / self.total_bytes)
        if self.is_tty:
            if not force and now - self.last_render_at < 0.5:
                return
            self.last_render_at = now
            width = max(20, min(40, shutil.get_terminal_size((100, 20)).columns - 70))
            filled = int(width * percent / 100)
            bar = "=" * filled + (">" if filled < width else "") + "." * max(width - filled - 1, 0)
            rate = consumed_bytes / max(now - self.started_at, 0.001)
            eta = (self.total_bytes - consumed_bytes) / rate if rate > 0 else 0
            status = "FAILED" if failed else "uploading"
            line = (
                f"\r{status} [{bar}] {percent:6.2f}% "
                f"{_format_bytes(consumed_bytes)}/{_format_bytes(self.total_bytes)} "
                f"{_format_bytes(rate)}/s ETA {_format_duration(eta)}"
            )
            sys.stderr.write(line[: shutil.get_terminal_size((100, 20)).columns - 1])
            sys.stderr.flush()
            return

        percent_int = int(percent)
        if force or failed or percent_int >= self.last_logged_percent + 5:
            self.last_logged_percent = percent_int
            LOGGER.info(
                "upload progress %s: %.2f%% %s/%s",
                self.label,
                percent,
                _format_bytes(consumed_bytes),
                _format_bytes(self.total_bytes),
            )


def _format_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
