#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${PVE_BACKUP_CONFIG:-/etc/pve-backup.yaml}"

UV_BIN="${UV_BIN:-}"
if [[ -z "$UV_BIN" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x /root/.local/bin/uv ]]; then
    UV_BIN="/root/.local/bin/uv"
  else
    echo "未找到 uv。请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "缺少 $PROJECT_DIR/.env" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "缺少配置文件: $CONFIG_PATH" >&2
  exit 1
fi

cd "$PROJECT_DIR"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_DIR/.uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PROJECT_DIR/.uv-python}"
"$UV_BIN" run pve-backup --config "$CONFIG_PATH" run "$@"
