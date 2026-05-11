from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import yaml

from .archive import create_encrypted_archive
from .config import load_config
from .naming import caesar_encrypt_filename
from .retention import delete_uploaded_files, prune_local, prune_remote
from .runner import build_vzdump_command, run_vzdump
from .state import read_state, write_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pve-backup")
    parser.add_argument("--config", type=Path, default=Path("/etc/pve-backup.yaml"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate")
    subparsers.add_parser("status")
    subparsers.add_parser("list-guests")
    subparsers.add_parser("local-history")
    subparsers.add_parser("remote-files")

    configure_parser = subparsers.add_parser("configure")
    configure_parser.add_argument("--vmids", help="comma-separated VM/CT ids, for example: 101,102")
    configure_parser.add_argument("--all", action="store_true", help="back up all guests")
    configure_parser.add_argument("--exclude", help="comma-separated guest ids to exclude with --all")
    configure_parser.add_argument("--dumpdir", type=Path, help="directory for temporary vzdump output")
    configure_parser.add_argument("--on-calendar", help="raw systemd OnCalendar expression")
    configure_parser.add_argument("--frequency", choices=["hourly", "daily", "weekly", "monthly"])
    configure_parser.add_argument("--time", default="03:20", help="HH:MM for daily/weekly/monthly")
    configure_parser.add_argument("--local-keep", type=int)
    configure_parser.add_argument("--remote-keep", type=int)
    configure_parser.add_argument(
        "--timer",
        type=Path,
        default=Path("systemd/pve-backup.timer"),
        help="timer file to update",
    )

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    load_dotenv(Path(".env"))

    if args.command == "configure":
        configure(args)
        return 0
    if args.command == "list-guests":
        list_guests()
        return 0

    config = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, config.runtime.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "validate":
        print("configuration OK")
        return 0
    if args.command == "status":
        show_status(config.runtime.state_file)
        return 0
    if args.command == "local-history":
        show_local_history(config)
        return 0
    if args.command == "remote-files":
        show_remote_files(config)
        return 0
    if args.command == "run":
        return run(config, dry_run=args.dry_run)
    return 2


def run(config, dry_run: bool = False) -> int:
    with single_instance(config.runtime.lock_file):
        if dry_run:
            print("vzdump command:", " ".join(build_vzdump_command(config.backup)))

        try:
            artifacts = run_vzdump(config.backup, dry_run=dry_run)
        except Exception as exc:
            if not dry_run:
                write_state(
                    config.runtime.state_file,
                    {
                        "status": "failed",
                        "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "error": str(exc),
                        "vzdump_command": build_vzdump_command(config.backup),
                    },
                )
            raise SystemExit(1) from exc
        if dry_run:
            return 0

        from .uploader import TosUploader

        uploader = TosUploader(config.tos)
        upload_artifact = (
            create_encrypted_archive(
                artifacts,
                config.archive,
                config.backup,
                config.tos,
                dry_run=dry_run,
            )
            if config.archive.enabled
            else None
        )
        upload_items = [upload_artifact] if upload_artifact else artifacts

        uploaded_paths = []
        uploaded_keys = []
        for artifact in upload_items:
            if artifact.path.suffix == ".log" and not config.tos.upload_logs:
                continue
            uploaded_keys.append(uploader.upload_artifact(artifact, dry_run=dry_run))
            uploaded_paths.append(artifact.path)

        prune_remote(uploader, config.tos.remote_keep_last_per_guest, dry_run=dry_run)
        if config.retention.delete_local_after_upload:
            delete_uploaded_files(
                uploaded_paths + [artifact.path for artifact in artifacts],
                dry_run=dry_run,
            )
        else:
            prune_local(
                config.backup.dumpdir,
                config.retention.local_keep_last_per_guest,
                upload_logs=config.tos.upload_logs,
                dry_run=dry_run,
            )
        write_state(
            config.runtime.state_file,
            {
                "status": "success",
                "uploaded_keys": uploaded_keys,
                "uploaded_paths": [str(path) for path in uploaded_paths],
                "remote_name_map": {
                    path.name: caesar_encrypt_filename(path.name) for path in uploaded_paths
                },
                "source_artifacts": [str(artifact.path) for artifact in artifacts],
                "vmids": sorted({artifact.vmid for artifact in artifacts if artifact.vmid}),
                "archive_enabled": config.archive.enabled,
            },
        )
    return 0


def configure(args) -> None:
    source = args.config if args.config.exists() else Path("config.example.yaml")
    if not source.exists():
        raise SystemExit(f"config template not found: {source}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}

    backup = raw.setdefault("backup", {})
    if args.all:
        backup["all"] = True
        backup["vmids"] = []
    elif args.vmids:
        backup["all"] = False
        backup["vmids"] = parse_ids(args.vmids)
    if args.exclude:
        backup["exclude"] = parse_ids(args.exclude)
    if args.dumpdir:
        backup["dumpdir"] = str(args.dumpdir)
    backup["mode"] = "snapshot"

    retention = raw.setdefault("retention", {})
    retention["delete_local_after_upload"] = True
    retention["local_keep_last_per_guest"] = 0

    tos = raw.setdefault("tos", {})
    if args.remote_keep is not None:
        tos["remote_keep_last_per_guest"] = args.remote_keep

    runtime = raw.setdefault("runtime", {})
    runtime["require_snapshot_mode"] = True

    archive = raw.setdefault("archive", {})
    archive["enabled"] = True
    archive.setdefault("password_env", "PVE_BACKUP_ARCHIVE_PASSWORD")

    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"wrote config: {args.config}")

    on_calendar = args.on_calendar or calendar_from_frequency(args.frequency, args.time)
    if on_calendar:
        update_timer(args.timer, on_calendar)
        print(f"wrote timer: {args.timer}")


def parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def calendar_from_frequency(frequency: str | None, time_value: str) -> str | None:
    if not frequency:
        return None
    if frequency == "hourly":
        return "hourly"
    hour_minute = time_value if len(time_value.split(":")) == 3 else f"{time_value}:00"
    if frequency == "daily":
        return f"*-*-* {hour_minute}"
    if frequency == "weekly":
        return f"Sun {hour_minute}"
    if frequency == "monthly":
        return f"*-*-01 {hour_minute}"
    raise ValueError(f"unsupported frequency: {frequency}")


def update_timer(path: Path, on_calendar: str) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "[Unit]",
            "Description=Nightly Proxmox VE backup upload",
            "",
            "[Timer]",
            "Persistent=true",
            "RandomizedDelaySec=20m",
            "",
            "[Install]",
            "WantedBy=timers.target",
        ]
    output = []
    replaced = False
    for line in lines:
        if line.startswith("OnCalendar="):
            output.append(f"OnCalendar={on_calendar}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        timer_index = output.index("[Timer]") if "[Timer]" in output else 0
        output.insert(timer_index + 1, f"OnCalendar={on_calendar}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def list_guests() -> None:
    commands = [("VM", "qm"), ("CT", "pct")]
    for label, command in commands:
        if not shutil.which(command):
            print(f"{label}: {command} not found")
            continue
        result = subprocess.run(
            [command, "list"],
            check=False,
            text=True,
            capture_output=True,
        )
        print(f"\n{label}:")
        print(result.stdout.strip() or result.stderr.strip() or "no output")


def show_status(path: Path) -> None:
    state = read_state(path)
    if not state:
        print(f"no backup state found at {path}")
        return
    print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def show_local_history(config) -> None:
    dumpdir = config.backup.dumpdir
    archive_dir = config.archive.output_dir or dumpdir
    paths = []
    for directory in {dumpdir, archive_dir}:
        if not directory.exists():
            continue
        paths.extend(
            path
            for path in directory.iterdir()
            if path.is_file()
            and (
                path.name.startswith("vzdump-")
                or path.name.startswith("pve-backup-")
            )
        )
    if not paths:
        print(f"未找到本地备份文件，检查目录: {dumpdir}")
        return
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        size_mb = stat.st_size / 1024 / 1024
        print(f"{path}\t{size_mb:.2f} MiB")


def show_remote_files(config) -> None:
    from .uploader import TosUploader

    uploader = TosUploader(config.tos)
    prefix = config.tos.prefix.format(hostname=__import__("socket").gethostname()).strip("/")
    if prefix:
        prefix = f"{prefix}/"
    keys = uploader.list_keys(prefix)
    if not keys:
        print(f"未找到远程备份文件: tos://{config.tos.bucket}/{prefix}")
        return
    for key in keys:
        print(f"tos://{config.tos.bucket}/{key}")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@contextmanager
def single_instance(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another backup process holds {lock_file}", file=sys.stderr)
            raise SystemExit(75)
        yield


if __name__ == "__main__":
    raise SystemExit(main())
