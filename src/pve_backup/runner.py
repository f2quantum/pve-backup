from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import BackupConfig

LOGGER = logging.getLogger(__name__)
BACKUP_RE = re.compile(
    r"^(?P<base>vzdump-(?P<kind>qemu|lxc|openvz)-(?P<vmid>\d+)-"
    r"\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})"
)


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    kind: str
    vmid: int


def build_vzdump_command(config: BackupConfig) -> list[str]:
    cmd = ["vzdump"]
    if config.all:
        cmd += ["--all", "1"]
        if config.exclude:
            cmd += ["--exclude", ",".join(str(vmid) for vmid in config.exclude)]
    else:
        cmd += [str(vmid) for vmid in config.vmids]

    cmd += [
        "--dumpdir",
        str(config.dumpdir),
        "--mode",
        config.mode,
        "--compress",
        config.compress,
        "--ionice",
        str(config.ionice),
    ]
    if config.bwlimit > 0:
        cmd += ["--bwlimit", str(config.bwlimit)]
    cmd += config.extra_args
    return cmd


def run_vzdump(config: BackupConfig, dry_run: bool = False) -> list[BackupArtifact]:
    start_time = time.time()
    cmd = build_vzdump_command(config)
    LOGGER.info("running: %s", " ".join(cmd))

    if dry_run:
        return []

    config.dumpdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        cmd,
        check=True,
        timeout=config.timeout_seconds or None,
    )
    return find_new_artifacts(config.dumpdir, start_time)


def find_new_artifacts(dumpdir: Path, start_time: float) -> list[BackupArtifact]:
    artifacts: list[BackupArtifact] = []
    for path in dumpdir.iterdir():
        if not path.is_file():
            continue
        if path.stat().st_mtime + 2 < start_time:
            continue
        match = BACKUP_RE.match(path.name)
        if not match:
            continue
        artifacts.append(
            BackupArtifact(
                path=path,
                kind=match.group("kind"),
                vmid=int(match.group("vmid")),
            )
        )
    artifacts.sort(key=lambda artifact: artifact.path.stat().st_mtime)
    return artifacts


def iter_guest_backup_sets(
    dumpdir: Path,
    upload_logs: bool,
) -> dict[tuple[str, int], dict[str, list[Path]]]:
    grouped: dict[tuple[str, int], dict[str, list[Path]]] = {}
    for path in dumpdir.iterdir():
        if not path.is_file():
            continue
        if not upload_logs and path.suffix == ".log":
            continue
        match = BACKUP_RE.match(path.name)
        if not match:
            continue
        key = (match.group("kind"), int(match.group("vmid")))
        grouped.setdefault(key, {}).setdefault(match.group("base"), []).append(path)

    for backup_sets in grouped.values():
        for files in backup_sets.values():
            files.sort(key=lambda item: item.name)
    return grouped
