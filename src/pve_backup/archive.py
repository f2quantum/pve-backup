from __future__ import annotations

import logging
import os
import re
import socket
import struct
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import pyzipper
import zstandard as zstd

from .config import ArchiveConfig, BackupConfig, TosConfig
from .runner import BackupArtifact

LOGGER = logging.getLogger(__name__)
MAGIC = b"PVEBK1\n"
PBKDF2_ITERATIONS = 600_000


def create_encrypted_archive(
    artifacts: list[BackupArtifact],
    archive_config: ArchiveConfig,
    backup_config: BackupConfig,
    tos_config: TosConfig,
    dry_run: bool = False,
) -> BackupArtifact:
    if not artifacts:
        raise ValueError("no backup artifacts were produced")

    files = [
        artifact.path
        for artifact in artifacts
        if tos_config.upload_logs or artifact.path.suffix != ".log"
    ]
    if not files:
        raise ValueError("no backup artifacts left after filtering logs")

    output_dir = archive_config.output_dir or backup_config.dumpdir
    timestamp = datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
    guest_label = _guest_label(artifacts)
    suffix = ".zip" if archive_config.format == "zip" else ".tar.zst.enc"
    archive_path = output_dir / f"pve-backup-{socket.gethostname()}-{guest_label}-{timestamp}{suffix}"

    if dry_run:
        LOGGER.info("would create encrypted archive %s", archive_path)
        return BackupArtifact(path=archive_path, kind="archive", vmid=0)

    output_dir.mkdir(parents=True, exist_ok=True)
    password = archive_config.password
    if not password:
        raise ValueError(f"missing archive password in {archive_config.password_env}")

    LOGGER.info("creating encrypted archive %s", archive_path)
    if archive_config.format == "tar_zst_enc":
        _create_tar_zst_enc(files, archive_path, password, archive_config.compression_level)
    else:
        _create_zip(files, archive_path, password, archive_config.compression_level)

    return BackupArtifact(path=archive_path, kind="archive", vmid=0)


def _create_zip(files: list[Path], archive_path: Path, password: str, compression_level: int) -> None:
    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
        compresslevel=compression_level,
    ) as zip_file:
        zip_file.setpassword(password.encode("utf-8"))
        for path in files:
            zip_file.write(path, arcname=path.name)


def _create_tar_zst_enc(
    files: list[Path],
    archive_path: Path,
    password: str,
    compression_level: int,
) -> None:
    salt = os.urandom(16)
    nonce = os.urandom(16)
    key_material = _derive_key(password.encode("utf-8"), salt)
    enc_key = key_material[:32]
    mac_key = key_material[32:]

    with archive_path.open("wb") as raw:
        header = MAGIC + salt + nonce + struct.pack(">I", PBKDF2_ITERATIONS)
        raw.write(header)
        mac = hmac.HMAC(mac_key, hashes.SHA256())
        mac.update(header)
        encrypted = _EncryptingWriter(raw, enc_key, nonce, mac)
        compressor = zstd.ZstdCompressor(level=compression_level).stream_writer(encrypted)
        try:
            with tarfile.open(fileobj=compressor, mode="w|") as tar:
                for path in files:
                    tar.add(path, arcname=path.name)
        finally:
            compressor.close()
        raw.write(mac.finalize())


def _derive_key(password: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=64,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password)


class _EncryptingWriter:
    def __init__(self, raw, key: bytes, nonce: bytes, mac: hmac.HMAC) -> None:
        self.raw = raw
        self.encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
        self.mac = mac

    def write(self, data: bytes) -> int:
        encrypted = self.encryptor.update(data)
        self.mac.update(encrypted)
        self.raw.write(encrypted)
        return len(data)

    def flush(self) -> None:
        self.raw.flush()

    def close(self) -> None:
        final = self.encryptor.finalize()
        if final:
            self.mac.update(final)
            self.raw.write(final)


def _guest_label(artifacts: list[BackupArtifact]) -> str:
    labels = []
    seen = set()
    for artifact in artifacts:
        if artifact.vmid == 0 or artifact.path.suffix == ".log":
            continue
        key = (artifact.kind, artifact.vmid)
        if key in seen:
            continue
        seen.add(key)
        name = _guest_name(artifact.kind, artifact.vmid)
        label = f"{artifact.vmid}-{name}" if name else str(artifact.vmid)
        labels.append(_safe_name(label))
    return "-".join(labels) or "all"


def _guest_name(kind: str, vmid: int) -> str | None:
    if kind == "qemu":
        command = ["qm", "config", str(vmid)]
        field = "name"
    elif kind == "lxc":
        command = ["pct", "config", str(vmid)]
        field = "hostname"
    else:
        return None
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    prefix = f"{field}:"
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    value = value.strip(".-")
    return value[:80] or "unknown"
