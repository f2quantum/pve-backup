from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .runner import iter_guest_backup_sets

if TYPE_CHECKING:
    from .uploader import TosUploader

LOGGER = logging.getLogger(__name__)
REMOTE_RE = re.compile(
    r"(?P<base>.*vzdump-(?P<kind>qemu|lxc|openvz)-(?P<vmid>\d+)-"
    r"\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})"
)


def prune_local(
    dumpdir: Path,
    keep_last_per_guest: int,
    upload_logs: bool,
    dry_run: bool = False,
) -> None:
    if keep_last_per_guest <= 0:
        return
    grouped = iter_guest_backup_sets(dumpdir, upload_logs=upload_logs)
    for backup_sets in grouped.values():
        bases = sorted(backup_sets, reverse=True)
        for base in bases[keep_last_per_guest:]:
            for path in backup_sets[base]:
                if dry_run:
                    LOGGER.info("would delete local %s", path)
                    continue
                LOGGER.info("deleting local %s", path)
                path.unlink()


def delete_uploaded_files(paths: list[Path], dry_run: bool = False) -> None:
    for path in paths:
        if dry_run:
            LOGGER.info("would delete local uploaded file %s", path)
            continue
        LOGGER.info("deleting local uploaded file %s", path)
        path.unlink(missing_ok=True)


def prune_remote(uploader: "TosUploader", keep_last_per_guest: int, dry_run: bool = False) -> None:
    if keep_last_per_guest <= 0:
        return
    prefix = uploader.config.prefix.format(hostname=__import__("socket").gethostname()).strip("/")
    if prefix:
        prefix = f"{prefix}/"
    keys = uploader.list_keys(prefix)

    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for key in keys:
        match = REMOTE_RE.match(key)
        if not match:
            continue
        grouped.setdefault((match.group("kind"), match.group("vmid")), {}).setdefault(
            match.group("base"),
            [],
        ).append(key)

    for backup_sets in grouped.values():
        bases = sorted(backup_sets, reverse=True)
        for base in bases[keep_last_per_guest:]:
            for key in backup_sets[base]:
                uploader.delete_key(key, dry_run=dry_run)
