#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="/etc/pve-backup.yaml"
TIMER_PATH="/etc/systemd/system/pve-backup.timer"
SERVICE_PATH="/etc/systemd/system/pve-backup.service"

usage() {
  cat <<'EOF'
用法: ./configure.sh [选项]

不带参数运行时进入中文菜单。

选项:
  --config PATH            配置文件路径。默认: /etc/pve-backup.yaml
  --vmids IDS              非交互配置：要备份的 VM/CT ID，例如: 101,102
  --all                    非交互配置：备份所有 VM/CT
  --exclude IDS            非交互配置：使用 --all 时排除的 ID
  --dumpdir PATH           非交互配置：vzdump 临时备份目录
  --frequency VALUE        非交互配置：hourly、daily、weekly、monthly
  --time HH:MM             非交互配置：执行时间。默认: 03:20
  --on-calendar VALUE      非交互配置：systemd OnCalendar 表达式
  --remote-keep N          非交互配置：远端保留数量
  --run-now                非交互配置后立即手动备份一次
  -h, --help               显示帮助

TOS 凭证和压缩包密码从项目目录 .env 读取。
本脚本不会打印 .env 内容。
EOF
}

ARGS=()
RUN_NOW=0
NON_INTERACTIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --run-now)
      RUN_NOW=1
      NON_INTERACTIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --vmids|--exclude|--dumpdir|--frequency|--time|--on-calendar|--remote-keep)
      ARGS+=("$1" "$2")
      NON_INTERACTIVE=1
      shift 2
      ;;
    --all)
      ARGS+=("$1")
      NON_INTERACTIVE=1
      shift
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "请在 PVE 主机上使用 root 用户运行本脚本。" >&2
    exit 1
  fi
}

find_uv() {
  if [[ -n "${UV_BIN:-}" ]]; then
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x /root/.local/bin/uv ]]; then
    UV_BIN="/root/.local/bin/uv"
  else
    echo "未找到 uv。请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
}

ensure_env() {
  if [[ -f "$PROJECT_DIR/.env" ]]; then
    return
  fi
  if [[ -f "$PROJECT_DIR/pve-backup.env.example" ]]; then
    cp "$PROJECT_DIR/pve-backup.env.example" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    echo "已根据示例创建 $PROJECT_DIR/.env。请先编辑真实凭证和压缩包密码。" >&2
  else
    echo "缺少 $PROJECT_DIR/.env" >&2
  fi
  exit 1
}

ensure_config() {
  if [[ ! -f "$CONFIG_PATH" ]]; then
    install -m 600 "$PROJECT_DIR/config.example.yaml" "$CONFIG_PATH"
    echo "已安装配置模板: $CONFIG_PATH"
  fi
}

sync_env() {
  cd "$PROJECT_DIR"
  export UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_DIR/.uv-cache}"
  export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PROJECT_DIR/.uv-python}"
  "$UV_BIN" sync
}

install_systemd() {
  chmod 755 "$PROJECT_DIR/start.sh"
  install -m 644 "$PROJECT_DIR/systemd/pve-backup.service" "$SERVICE_PATH"
  install -m 644 "$PROJECT_DIR/systemd/pve-backup.timer" "$TIMER_PATH"
  systemctl daemon-reload
  systemctl enable --now pve-backup.timer
}

run_backup_now() {
  local manual_config
  manual_config="$(mktemp /tmp/pve-backup-manual.XXXXXX.yaml)"
  cp "$CONFIG_PATH" "$manual_config"

  local manual_args=()
  echo
  echo "当前 PVE VM/CT 列表:"
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" list-guests || true

  echo
  echo "请选择本次手动备份目标:"
  echo "  1) 指定 VM/CT ID"
  echo "  2) 备份所有 VM/CT"
  read -r -p "请选择 [1]: " target_choice
  target_choice="${target_choice:-1}"
  if [[ "$target_choice" == "2" ]]; then
    manual_args+=("--all")
    read -r -p "需要排除的 ID，逗号分隔，可留空: " exclude_ids
    if [[ -n "$exclude_ids" ]]; then
      manual_args+=("--exclude" "$exclude_ids")
    fi
  else
    read -r -p "请输入 VM/CT ID，逗号分隔 [101]: " vmids
    vmids="${vmids:-101}"
    manual_args+=("--vmids" "$vmids")
  fi

  current_dumpdir="$(grep -E '^[[:space:]]*dumpdir:' "$CONFIG_PATH" | head -n 1 | awk '{print $2}')"
  current_dumpdir="${current_dumpdir:-/var/lib/vz/dump}"
  read -r -p "请输入本次备份临时目录 [$current_dumpdir]: " manual_dumpdir
  manual_dumpdir="${manual_dumpdir:-$current_dumpdir}"
  manual_args+=("--dumpdir" "$manual_dumpdir")

  "$UV_BIN" run pve-backup --config "$manual_config" configure \
    "${manual_args[@]}"
  "$UV_BIN" run pve-backup --config "$manual_config" validate

  echo
  echo "正在手动执行一次备份..."
  PVE_BACKUP_CONFIG="$manual_config" "$PROJECT_DIR/start.sh"
  rm -f "$manual_config"
  echo "手动备份执行完成。"
}

configure_auto_backup() {
  local config_args=()

  echo
  echo "当前 PVE VM/CT 列表:"
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" list-guests || true

  echo
  echo "请选择备份目标:"
  echo "  1) 指定 VM/CT ID"
  echo "  2) 备份所有 VM/CT"
  read -r -p "请选择 [1]: " target_choice
  target_choice="${target_choice:-1}"
  if [[ "$target_choice" == "2" ]]; then
    config_args+=("--all")
    read -r -p "需要排除的 ID，逗号分隔，可留空: " exclude_ids
    if [[ -n "$exclude_ids" ]]; then
      config_args+=("--exclude" "$exclude_ids")
    fi
  else
    read -r -p "请输入 VM/CT ID，逗号分隔 [101]: " vmids
    vmids="${vmids:-101}"
    config_args+=("--vmids" "$vmids")
  fi

  echo
  echo "请选择备份频率:"
  echo "  1) 每天"
  echo "  2) 每周"
  echo "  3) 每月"
  echo "  4) 每小时"
  echo "  5) 自定义 systemd OnCalendar"
  read -r -p "请选择 [1]: " schedule_choice
  schedule_choice="${schedule_choice:-1}"
  case "$schedule_choice" in
    2) frequency="weekly" ;;
    3) frequency="monthly" ;;
    4) frequency="hourly" ;;
    5)
      read -r -p "请输入 OnCalendar [*-*-* 03:20:00]: " on_calendar
      on_calendar="${on_calendar:-*-*-* 03:20:00}"
      config_args+=("--on-calendar" "$on_calendar")
      frequency=""
      ;;
    *) frequency="daily" ;;
  esac
  if [[ -n "${frequency:-}" ]]; then
    config_args+=("--frequency" "$frequency")
    if [[ "$frequency" != "hourly" ]]; then
      read -r -p "请输入执行时间 HH:MM [03:20]: " run_time
      run_time="${run_time:-03:20}"
      config_args+=("--time" "$run_time")
    fi
  fi

  current_dumpdir="$(grep -E '^[[:space:]]*dumpdir:' "$CONFIG_PATH" | head -n 1 | awk '{print $2}')"
  current_dumpdir="${current_dumpdir:-/var/lib/vz/dump}"
  echo
  echo "请选择 vzdump 临时备份目录。"
  echo "上传 TOS 成功后，本地备份文件会自动删除。"
  read -r -p "备份临时目录 [$current_dumpdir]: " dumpdir
  dumpdir="${dumpdir:-$current_dumpdir}"
  config_args+=("--dumpdir" "$dumpdir")

  read -r -p "每个 guest 在 TOS 远端保留多少组备份 [7]: " remote_keep
  remote_keep="${remote_keep:-7}"
  config_args+=("--remote-keep" "$remote_keep")

  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" configure \
    "${config_args[@]}" \
    --timer "$PROJECT_DIR/systemd/pve-backup.timer"
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" validate
  install_systemd
  echo "自动备份配置完成。"
}

show_local_history() {
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" status || true
  echo
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" local-history || true
}

show_remote_files() {
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" remote-files
}

set_env_value() {
  local key="$1"
  local value="$2"
  local env_file="$PROJECT_DIR/.env"
  local tmp_file
  tmp_file="$(mktemp /tmp/pve-backup-env.XXXXXX)"
  if [[ -f "$env_file" ]]; then
    grep -v "^${key}=" "$env_file" > "$tmp_file" || true
  fi
  printf '%s=%s\n' "$key" "$value" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
}

get_env_value() {
  local key="$1"
  local env_file="$PROJECT_DIR/.env"
  grep "^${key}=" "$env_file" | tail -n 1 | cut -d= -f2-
}

mask_value() {
  local value="$1"
  local length="${#value}"
  if [[ "$length" -le 8 ]]; then
    echo "********"
  else
    echo "${value:0:4}****${value: -4}"
  fi
}

show_secrets_masked() {
  local ak sk password
  ak="$(get_env_value "TOS_ACCESS_KEY")"
  sk="$(get_env_value "TOS_SECRET_KEY")"
  password="$(get_env_value "PVE_BACKUP_ARCHIVE_PASSWORD")"
  echo "TOS_ACCESS_KEY=$(mask_value "$ak")"
  echo "TOS_SECRET_KEY=$(mask_value "$sk")"
  echo "PVE_BACKUP_ARCHIVE_PASSWORD=$(mask_value "$password")"
}

show_secrets_plain() {
  echo "TOS_ACCESS_KEY=$(get_env_value "TOS_ACCESS_KEY")"
  echo "TOS_SECRET_KEY=$(get_env_value "TOS_SECRET_KEY")"
  echo "PVE_BACKUP_ARCHIVE_PASSWORD=$(get_env_value "PVE_BACKUP_ARCHIVE_PASSWORD")"
}

secrets_menu() {
  echo
  echo "当前密钥和压缩包密码（默认脱敏显示）:"
  show_secrets_masked
  read -r -p "是否显示明文？输入 y 确认 [n]: " reveal_answer
  case "${reveal_answer:-n}" in
    y|Y|yes|YES)
      echo
      echo "明文内容:"
      show_secrets_plain
      ;;
  esac

  echo
  read -r -p "是否修改 AK/SK 和压缩包密码？输入 y 确认 [n]: " modify_answer
  case "${modify_answer:-n}" in
    y|Y|yes|YES) ;;
    *) return ;;
  esac

  echo "留空表示不修改。"
  read -r -p "新的 TOS_ACCESS_KEY: " new_ak
  read -r -s -p "新的 TOS_SECRET_KEY: " new_sk
  echo
  read -r -s -p "新的 PVE_BACKUP_ARCHIVE_PASSWORD: " new_password
  echo
  if [[ -n "$new_ak" ]]; then
    set_env_value "TOS_ACCESS_KEY" "$new_ak"
  fi
  if [[ -n "$new_sk" ]]; then
    set_env_value "TOS_SECRET_KEY" "$new_sk"
  fi
  if [[ -n "$new_password" ]]; then
    set_env_value "PVE_BACKUP_ARCHIVE_PASSWORD" "$new_password"
  fi
  chmod 600 "$PROJECT_DIR/.env"
  echo "已更新 .env。"
}

main_menu() {
  while true; do
    echo
    echo "请选择当前操作:"
    echo "  1) 手动备份"
    echo "  2) 配置自动备份"
    echo "  3) 查看本地备份历史"
    echo "  4) 查看远程备份文件"
    echo "  5) 查看/修改 AK/SK 和压缩包密码"
    echo "  0) 退出"
    read -r -p "请输入选项 [0]: " action
    action="${action:-0}"
    case "$action" in
      1) run_backup_now ;;
      2) configure_auto_backup ;;
      3) show_local_history ;;
      4) show_remote_files ;;
      5) secrets_menu ;;
      0) exit 0 ;;
      *) echo "无效选项: $action" >&2 ;;
    esac
  done
}

require_root
find_uv
ensure_env
ensure_config
sync_env

if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" configure \
    "${ARGS[@]}" \
    --timer "$PROJECT_DIR/systemd/pve-backup.timer"
  "$UV_BIN" run pve-backup --config "$CONFIG_PATH" validate
  install_systemd
  if [[ "$RUN_NOW" -eq 1 ]]; then
    run_backup_now
  fi
  systemctl --no-pager status pve-backup.timer
  exit 0
fi

main_menu
