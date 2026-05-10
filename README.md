# pve-backup

`pve-backup` 是一个运行在 Proxmox VE 主机上的 Python 备份工具。它调用 PVE 原生 `vzdump` 对虚拟机执行不停机快照备份，把本次备份产物打成带密码的 AES ZIP 压缩包，然后上传到火山引擎 TOS。

项目边界很明确：PVE 负责真正的 VM/CT 备份；本项目负责调度入口、单实例锁、加密打包、TOS 上传、本地和远端保留策略。

## 关键特性

- 使用 PVE 官方 `vzdump`，不在 Python 中重写备份逻辑。
- 默认强制 `snapshot` 模式，避免误配置成 `stop` 或 `suspend` 导致虚拟机停机。
- 每次备份后生成一个带密码的 AES ZIP，只上传这个压缩包到 TOS。
- 上传到 TOS 时会用固定偏移量的 Caesar 编码隐藏对象文件名语义；本地 ZIP 文件名保持可读。
- 使用 `uv` 管理 Python 环境和依赖。
- 上传到火山引擎 TOS，默认目标为 `tos://bytehouse-engine-qa-test/backup/{hostname}/`。
- 凭证从项目目录 `.env` 读取，`.env` 已被 `.gitignore` 忽略。
- 通过 systemd timer 定时运行，不需要常驻 Python 进程。

## 不停机备份说明

默认配置如下：

```yaml
backup:
  mode: snapshot

runtime:
  require_snapshot_mode: true
```

`runtime.require_snapshot_mode: true` 会让 CLI 在发现 `backup.mode` 不是 `snapshot` 时直接失败。这样可以避免把备份模式误改成 `stop` 或 `suspend` 后影响正在运行的虚拟机。

建议给虚拟机安装并启用 QEMU Guest Agent。`snapshot` 模式可以在线备份运行中的 VM；Guest Agent 能帮助 PVE 在备份时冻结/解冻 guest 文件系统，提高应用和文件系统一致性。

PVE 官方文档：

- Backup and Restore: https://pve.proxmox.com/pve-docs/chapter-vzdump.html
- `vzdump(1)`: https://pve.proxmox.com/pve-docs/vzdump.1.html

## 项目结构

```text
.
├── config.example.yaml          # 配置示例
├── pve-backup.env.example       # .env 示例，不含真实凭证
├── pyproject.toml               # Python 项目和 uv 依赖声明
├── uv.lock                      # uv 锁文件
├── src/pve_backup/
│   ├── cli.py                   # CLI、锁、.env 加载
│   ├── config.py                # 配置解析和校验
│   ├── runner.py                # vzdump 命令构造和产物发现
│   ├── archive.py               # 加密 ZIP 打包
│   ├── uploader.py              # 火山引擎 TOS 上传/删除/列举
│   ├── retention.py             # 本地和远端保留策略
│   └── state.py                 # 上次备份状态
└── systemd/
    ├── pve-backup.service
    └── pve-backup.timer
```

## 安装

在 PVE 主机上执行：

```bash
cd /root/pve-backup
apt update
apt install -y curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync
```

如果 `uv` 已安装但不在 PATH 中，可以直接使用：

```bash
/root/.local/bin/uv sync
```

也可以使用根目录的配置脚本完成环境同步、配置模板安装、systemd timer 安装和启用：

```bash
bash configure.sh
```

不带参数时，脚本会进入中文菜单：

```text
1) 手动备份
2) 配置自动备份
3) 查看本地备份历史
4) 查看远程备份文件
5) 查看/修改 AK/SK 和压缩包密码
0) 退出
```

选择“配置自动备份”时，会先展示当前 PVE VM/CT，再让你选择要备份的虚拟机、备份频率、本地/远端保留数量。

非交互式配置也支持：

```bash
./configure.sh --vmids 101,102 --frequency daily --time 03:20
```

如果要安装后立刻执行一次备份：

```bash
./configure.sh --vmids 101,102 --frequency daily --time 03:20 --run-now
```

交互模式下也会询问是否立即手动备份一次。手动备份会先让你选择本次要备份的 VM/CT，然后使用临时配置直接执行 `./start.sh` 并实时显示输出；不会覆盖自动备份配置。systemd 只负责定时触发。

## 配置

复制配置文件：

```bash
cp config.example.yaml /etc/pve-backup.yaml
chmod 600 /etc/pve-backup.yaml
```

编辑 `/etc/pve-backup.yaml`，至少确认这些字段：

```yaml
backup:
  vmids: [101, 102]
  all: false
  dumpdir: /var/lib/vz/dump
  mode: snapshot

tos:
  endpoint: tos-cn-beijing.volces.com
  region: cn-beijing
  bucket: bytehouse-engine-qa-test
  prefix: backup/{hostname}
```

写入 TOS 凭证到项目目录 `.env`：

```bash
cp pve-backup.env.example .env
chmod 600 .env
```

`.env` 格式：

```dotenv
TOS_ACCESS_KEY=...
TOS_SECRET_KEY=...
PVE_BACKUP_ARCHIVE_PASSWORD=...
```

不要把 AK/SK 或压缩包密码写入 YAML 配置、README 或提交记录。

## CLI 管理

列出当前 PVE 上的 VM 和 CT：

```bash
uv run pve-backup --config /etc/pve-backup.yaml list-guests
```

配置要备份的虚拟机、每天执行时间和保留数量：

```bash
uv run pve-backup --config /etc/pve-backup.yaml configure \
  --vmids 101,102 \
  --frequency daily \
  --time 03:30 \
  --local-keep 2 \
  --remote-keep 7 \
  --timer /etc/systemd/system/pve-backup.timer
```

备份所有 guest，并排除指定 ID：

```bash
uv run pve-backup --config /etc/pve-backup.yaml configure \
  --all \
  --exclude 100,105 \
  --frequency weekly \
  --time 04:00 \
  --timer /etc/systemd/system/pve-backup.timer
```

支持的 `--frequency`：`hourly`、`daily`、`weekly`、`monthly`。也可以直接传 systemd 的 `OnCalendar` 表达式：

```bash
uv run pve-backup --config /etc/pve-backup.yaml configure \
  --vmids 101,102 \
  --on-calendar "*-*-* 03:20:00" \
  --timer /etc/systemd/system/pve-backup.timer
```

## 使用

验证配置：

```bash
uv run pve-backup --config /etc/pve-backup.yaml validate
```

预览将要执行的 `vzdump` 命令：

```bash
uv run pve-backup --config /etc/pve-backup.yaml run --dry-run
```

执行一次备份和上传：

```bash
uv run pve-backup --config /etc/pve-backup.yaml run
```

查看上次备份状态：

```bash
uv run pve-backup --config /etc/pve-backup.yaml status
```

状态默认存放在 `/var/lib/pve-backup/state.json`。

## 加密压缩包

默认配置：

```yaml
archive:
  enabled: true
  output_dir:
  password_env: PVE_BACKUP_ARCHIVE_PASSWORD
  compression_level: 6
```

运行流程：

```text
vzdump snapshot backup -> encrypted zip -> upload zip to TOS
```

上传到 TOS 的对象是单个 `.zip` 文件，里面包含本次 `vzdump` 生成的备份归档和日志。ZIP 使用 `.env` 中 `PVE_BACKUP_ARCHIVE_PASSWORD` 指定的密码加密。

远程 TOS 对象名会使用固定偏移量 Caesar 编码处理，但日期数字不编码，方便看出是哪天生成的。例如本地文件名保持可读，上传后的对象名会变成编码后的字符串；映射关系会写入 `/var/lib/pve-backup/state.json` 的 `remote_name_map`。

## 定时运行

安装 systemd unit：

```bash
cp systemd/pve-backup.service /etc/systemd/system/
cp systemd/pve-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pve-backup.timer
```

systemd timer 触发的是 `/root/pve-backup/start.sh`，这个脚本会进入项目目录并执行：

```bash
uv run pve-backup --config /etc/pve-backup.yaml run
```

默认 timer 每天 03:20 运行，并带有 20 分钟随机延迟。修改 `systemd/pve-backup.timer` 中的 `OnCalendar=` 可调整执行时间。

手动触发：

```bash
systemctl start pve-backup.service
```

查看日志：

```bash
journalctl -u pve-backup.service -n 200 --no-pager
```

## 保留策略

本地保留：

```yaml
retention:
  local_keep_last_per_guest: 0
  delete_local_after_upload: true
```

默认行为是上传到 TOS 成功后删除本地文件，包括加密归档和本次 `vzdump` 生成的原始备份文件，避免 `/var/lib/vz/dump` 被长期占满。

远端 TOS 保留：

```yaml
tos:
  remote_keep_last_per_guest: 7
```

保留策略按每台 VM/CT 的每次备份为单位处理，会把同一时间戳的备份归档和日志视为同一组。

## 恢复

本项目上传的是带密码的 ZIP 压缩包。恢复时先从 TOS 下载 ZIP 到 PVE 主机，用 `.env` 中的 `PVE_BACKUP_ARCHIVE_PASSWORD` 解压，再使用 PVE 原生命令恢复解压出来的备份归档。

恢复 QEMU VM：

```bash
qmrestore /var/lib/vz/dump/vzdump-qemu-101-YYYY_MM_DD-HH_MM_SS.vma.zst 101
```

恢复 LXC：

```bash
pct restore 201 /var/lib/vz/dump/vzdump-lxc-201-YYYY_MM_DD-HH_MM_SS.tar.zst
```

## 注意事项

- 需要在 PVE 主机上以有权限执行 `vzdump` 的用户运行，通常是 root。
- 快照备份依赖底层存储和 PVE 能力；如果某个 guest 或存储后端不支持快照，需要先在 PVE 层处理。
- 当前项目目录中的 `.env`、`.venv`、`.uv-cache`、`.uv-python` 都是本地状态，不应提交。
