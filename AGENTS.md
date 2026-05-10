# AGENTS.md

本文件给后续维护这个仓库的编码代理或工程师使用。目标是保持项目行为稳定，尤其是“不停机备份 VM”和“凭证不进仓库”这两件事。

## 项目目标

`pve-backup` 在 Proxmox VE 主机上运行，调用 PVE 原生 `vzdump` 对虚拟机执行 `snapshot` 模式备份，把本次备份产物打成带密码的 AES ZIP，并把 ZIP 上传到火山引擎 TOS。

不要在 Python 里重写 PVE 的备份逻辑。Python 只负责：

- 配置解析和校验
- 构造并执行 `vzdump`
- 发现新生成的备份文件
- 把本次备份产物打成带密码的 AES ZIP
- 上传加密 ZIP 到 TOS，远程对象名使用固定 Caesar 偏移量编码，本地文件名保持可读
- 执行本地和远端保留策略
- 提供 CLI 和 systemd timer 集成

## 必须遵守的约束

- 必须使用 `uv` 管理环境和依赖，不要改回 `pip install -e .` 或手写 requirements。
- 默认备份模式必须保持 `backup.mode: snapshot`。
- `runtime.require_snapshot_mode` 默认必须为 `true`。
- 不要引入会导致 VM 停机或暂停的默认行为，例如 `stop` 或 `suspend`。
- 不要把 AK/SK、临时 token、真实密钥写入 README、示例配置、测试快照或提交内容。
- 不要把压缩包密码写入 README、示例配置、测试快照或提交内容。
- 真实凭证和压缩包密码只允许放在项目目录 `.env` 或部署环境变量中。
- `.env` 必须继续被 `.gitignore` 忽略。
- 不要提交 `.venv/`、`.uv-cache/`、`.uv-python/`。

## 常用命令

依赖同步：

```bash
uv sync
```

如果当前 shell 找不到 `uv`，本机常见路径是：

```bash
/root/.local/bin/uv sync
```

配置验证：

```bash
uv run pve-backup --config config.example.yaml validate
```

预览备份命令：

```bash
uv run pve-backup --config config.example.yaml run --dry-run
```

配置 VM 和定时频率：

```bash
uv run pve-backup --config /etc/pve-backup.yaml configure --vmids 101,102 --frequency daily --time 03:30 --timer /etc/systemd/system/pve-backup.timer
```

查看上次备份状态：

```bash
uv run pve-backup --config /etc/pve-backup.yaml status
```

语法检查：

```bash
python3 -m compileall src
```

## 重要文件

- `src/pve_backup/config.py`：配置模型和安全校验。涉及不停机备份约束时优先看这里。
- `src/pve_backup/runner.py`：`vzdump` 命令构造和备份产物发现。
- `src/pve_backup/archive.py`：带密码的 AES ZIP 打包。
- `src/pve_backup/uploader.py`：TOS SDK 调用。
- `src/pve_backup/retention.py`：本地和远端保留策略。
- `src/pve_backup/cli.py`：CLI、单实例锁、`.env` 加载。
- `config.example.yaml`：用户复制到 `/etc/pve-backup.yaml` 的配置模板。
- `systemd/`：定时运行入口。
- `configure.sh`：根目录中文菜单脚本，负责手动备份、自动备份配置、本地/远程历史查看、密钥修改、systemd timer 安装和启用。
- `start.sh`：根目录启动脚本，由 systemd service 触发，负责执行一次备份。

## 修改建议

涉及备份行为时，优先保持保守：

- 新增参数要有配置校验。
- 默认值不能扩大停机风险。
- `dry-run` 不应初始化 TOS 客户端或要求真实凭证。
- 上传前不要删除本地备份文件或加密 ZIP。
- 默认上传对象应是加密 ZIP，而不是原始 `vzdump` 产物。
- 不要改成本地文件名也加密；只隐藏远程 TOS 对象名语义。
- 删除逻辑必须按每台 guest 的每次备份分组处理，不能把归档和日志拆成独立备份计数。

## 验证清单

完成代码修改后至少运行：

```bash
python3 -m compileall src
uv run pve-backup --config config.example.yaml validate
```

如果修改了 `vzdump` 参数构造，还要运行：

```bash
uv run pve-backup --config config.example.yaml run --dry-run
```

注意：在受限沙箱中 `/var/lock` 可能不可写，`dry-run` 可能因为锁文件失败。真实 PVE 主机 root 环境通常可写。
