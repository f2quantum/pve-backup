from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BackupConfig:
    vmids: list[int] = field(default_factory=list)
    all: bool = False
    exclude: list[int] = field(default_factory=list)
    dumpdir: Path = Path("/var/lib/vz/dump")
    mode: str = "snapshot"
    compress: str = "zstd"
    bwlimit: int = 0
    ionice: int = 7
    timeout_seconds: int = 0
    extra_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TosConfig:
    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key_id_env: str = "TOS_ACCESS_KEY"
    secret_access_key_env: str = "TOS_SECRET_KEY"
    upload_logs: bool = True
    storage_class: str | None = None
    remote_keep_last_per_guest: int = 0

    @property
    def access_key_id(self) -> str | None:
        return os.environ.get(self.access_key_id_env)

    @property
    def secret_access_key(self) -> str | None:
        return os.environ.get(self.secret_access_key_env)


@dataclass(frozen=True)
class RetentionConfig:
    local_keep_last_per_guest: int = 0
    delete_local_after_upload: bool = False


@dataclass(frozen=True)
class ArchiveConfig:
    enabled: bool = True
    format: str = "tar_zst_enc"
    output_dir: Path | None = None
    password_env: str = "PVE_BACKUP_ARCHIVE_PASSWORD"
    compression_level: int = 19

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env)


@dataclass(frozen=True)
class RuntimeConfig:
    lock_file: Path = Path("/var/lock/pve-backup.lock")
    state_file: Path = Path("/var/lib/pve-backup/state.json")
    log_level: str = "INFO"
    require_snapshot_mode: bool = True


@dataclass(frozen=True)
class AppConfig:
    backup: BackupConfig
    tos: TosConfig
    archive: ArchiveConfig
    retention: RetentionConfig
    runtime: RuntimeConfig


def load_config(path: Path) -> AppConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

    backup = _backup_config(raw.get("backup", {}))
    tos = _tos_config(raw.get("tos", {}))
    archive = _archive_config(raw.get("archive", {}))
    retention = _retention_config(raw.get("retention", {}))
    runtime = _runtime_config(raw.get("runtime", {}))
    config = AppConfig(
        backup=backup,
        tos=tos,
        archive=archive,
        retention=retention,
        runtime=runtime,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.backup.all and config.backup.vmids:
        raise ValueError("backup.all and backup.vmids are mutually exclusive")
    if not config.backup.all and not config.backup.vmids:
        raise ValueError("set backup.vmids or backup.all=true")
    if config.backup.mode not in {"snapshot", "stop", "suspend"}:
        raise ValueError("backup.mode must be one of: snapshot, stop, suspend")
    if config.runtime.require_snapshot_mode and config.backup.mode != "snapshot":
        raise ValueError("non-disruptive VM backups require backup.mode=snapshot")
    if config.backup.compress not in {"0", "1", "gzip", "lzo", "zstd"}:
        raise ValueError("backup.compress must be one of: 0, 1, gzip, lzo, zstd")
    if not config.tos.endpoint:
        raise ValueError("tos.endpoint is required")
    if not config.tos.region:
        raise ValueError("tos.region is required")
    if not config.tos.bucket:
        raise ValueError("tos.bucket is required")
    if config.archive.enabled and not config.archive.password:
        raise ValueError(f"archive password is required in {config.archive.password_env}")
    if config.archive.format not in {"tar_zst_enc", "tar_enc", "zip"}:
        raise ValueError("archive.format must be one of: tar_zst_enc, tar_enc, zip")
    if not 0 <= config.archive.compression_level <= 9:
        if config.archive.format == "zip":
            raise ValueError("archive.compression_level must be between 0 and 9 for zip")
    if config.archive.format == "tar_zst_enc" and not 1 <= config.archive.compression_level <= 22:
        raise ValueError("archive.compression_level must be between 1 and 22 for tar_zst_enc")
    if config.retention.delete_local_after_upload and config.retention.local_keep_last_per_guest:
        raise ValueError("delete_local_after_upload conflicts with local_keep_last_per_guest")


def _backup_config(raw: dict[str, Any]) -> BackupConfig:
    return BackupConfig(
        vmids=[int(vmid) for vmid in raw.get("vmids", [])],
        all=bool(raw.get("all", False)),
        exclude=[int(vmid) for vmid in raw.get("exclude", [])],
        dumpdir=Path(raw.get("dumpdir", "/var/lib/vz/dump")),
        mode=str(raw.get("mode", "snapshot")),
        compress=str(raw.get("compress", "zstd")),
        bwlimit=int(raw.get("bwlimit", 0)),
        ionice=int(raw.get("ionice", 7)),
        timeout_seconds=int(raw.get("timeout_seconds", 0)),
        extra_args=[str(arg) for arg in raw.get("extra_args", [])],
    )


def _tos_config(raw: dict[str, Any]) -> TosConfig:
    return TosConfig(
        endpoint=str(raw.get("endpoint", "tos-cn-beijing.volces.com")),
        region=str(raw.get("region", "cn-beijing")),
        bucket=str(raw.get("bucket", "")),
        prefix=str(raw.get("prefix", "backup/{hostname}")).strip("/"),
        access_key_id_env=str(raw.get("access_key_id_env", "TOS_ACCESS_KEY")),
        secret_access_key_env=str(raw.get("secret_access_key_env", "TOS_SECRET_KEY")),
        upload_logs=bool(raw.get("upload_logs", True)),
        storage_class=raw.get("storage_class"),
        remote_keep_last_per_guest=int(raw.get("remote_keep_last_per_guest", 0)),
    )


def _retention_config(raw: dict[str, Any]) -> RetentionConfig:
    return RetentionConfig(
        local_keep_last_per_guest=int(raw.get("local_keep_last_per_guest", 0)),
        delete_local_after_upload=bool(raw.get("delete_local_after_upload", False)),
    )


def _archive_config(raw: dict[str, Any]) -> ArchiveConfig:
    output_dir = raw.get("output_dir")
    return ArchiveConfig(
        enabled=bool(raw.get("enabled", True)),
        format=str(raw.get("format", "tar_zst_enc")),
        output_dir=Path(output_dir) if output_dir else None,
        password_env=str(raw.get("password_env", "PVE_BACKUP_ARCHIVE_PASSWORD")),
        compression_level=int(raw.get("compression_level", 19)),
    )


def _runtime_config(raw: dict[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        lock_file=Path(raw.get("lock_file", "/var/lock/pve-backup.lock")),
        state_file=Path(raw.get("state_file", "/var/lib/pve-backup/state.json")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        require_snapshot_mode=bool(raw.get("require_snapshot_mode", True)),
    )
