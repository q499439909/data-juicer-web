# Web UI 完整部署指南

本仓库包含 Web UI 和配套的 Data-Juicer Agents 后端。运行关系：
浏览器 → FastAPI Web → Agents → Data-Juicer。前端是本地 HTML/CSS/JavaScript，
无需 npm 构建，不依赖远程 CDN。

## 1. 环境要求

- 推荐 Ubuntu 24.04 / Linux x86_64、Python 3.12；Windows 请使用 WSL2。
- Python 包支持 3.10–3.12，下述默认安装及验证使用 3.12。
- Git、Bash、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和包下载网络。
- 基本文本任务可以使用 CPU。图片、音视频及模型算子可能需要额外依赖、模型下载、FFmpeg 或 GPU。
- Agent 对话需要自己的有效模型 API Key；无 Key 也可启动、登录和运行部署自检。

安装 uv（或使用官方提供的其他安装方式）：

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

uv 自行创建虚拟环境，找不到目标 Python 时会尝试下载，无需系统 pip 或 python3-venv。

## 2. 克隆与安装

```bash
git clone https://github.com/q499439909/data-juicer-web.git
cd data-juicer-web
bash agent_web/install.sh
```

脚本执行 `uv sync --locked --extra web --no-dev --python 3.12`，
完整安装 Web 和 Agents 依赖到仓库的 .venv，并在首次安装时复制
agent_web/.env.example 到 agent_web/.env。已有配置不会覆盖。

关键版本为 AgentScope 1.0.14、Data-Juicer 1.5.5、Ray 2.57.0，
其余版本由 uv.lock 固定。Data-Juicer 从公开包索引安装，无需 ../data-juicer-1.5.4。
默认包含官方 CPU 版 PyTorch 2.8.0，避免首次执行时由 Data-Juicer 隐式下载未锁定的 CUDA 依赖。
安装需要同时访问 PyPI 和 download.pytorch.org；下载慢时可提高 UV_HTTP_TIMEOUT 并重试复用缓存。
GPU 模型算子需要按硬件配置 CUDA 版 PyTorch 和对应算子依赖；默认环境面向 CPU 处理。
不要复制 Windows 虚拟环境到 Linux。仓库及虚拟环境尽量放在 Linux 文件系统中。

无 uv 时，可先创建并激活 Python 3.12 虚拟环境，在仓库根目录执行
`python -m pip install -r agent_web/requirements.txt` 安装完整 .[web]。
该方式不会按 uv.lock 固定传递依赖，因此优先使用安装脚本。

## 3. 配置

编辑 agent_web/.env，至少填写自己的 Key：

```dotenv
DASHSCOPE_API_KEY=填写自己的Key
DJA_SESSION_MODEL=qwen3-max-2026-01-23
AGENT_WEB_HOST=0.0.0.0
AGENT_WEB_PORT=8090
AGENT_WEB_COOKIE_SECURE=false
AGENT_WEB_DATA_DIR=./agent_web/data
RAY_ADDRESS=http://127.0.0.1:8265
AGENT_WEB_RAY_AUTO_START=true
```

确认账号拥有该模型权限，不可用时修改模型名。DJA_OPENAI_BASE_URL 可配置
OpenAI 兼容端点，当前模型客户端使用 DASHSCOPE_API_KEY / MODELSCOPE_API_TOKEN
作为凭据；更换供应商后也应核对 DJA_PLANNER_MODEL 和参数兼容性。

.env 自动按 dotenv 格式加载，无需 source，也不会执行 shell 代码。
显式传入进程的环境变量优先。AGENT_WEB_ENV_FILE 可以选择其他配置文件。
直接运行 Python 模块也会加载配置。相对数据目录按工作目录解析，Bash 脚本均先进入仓库根目录；
生产部署建议设置绝对数据目录。

## 4. 管理员、启动与停止

```bash
bash agent_web/scripts/create_admin.sh admin
# 输入并确认密码，至少 8 位；不回显
bash agent_web/start.sh --background
bash agent_web/status.sh
```

访问 http://localhost:8090 或 http://服务器地址:8090，使用创建的管理员账号登录。
普通用户可以在注册页创建账号。管理员可查看运行、任务记录。
程序化创建可使用 --password-stdin 从标准输入读取一行密码，不要把真实密码放进命令行或 Git 文件。

```bash
bash agent_web/start.sh  # 前台运行，Ctrl+C 停止
bash agent_web/stop.sh   # 停止脚本启动的后台 Web
```

启动固定使用仓库 .venv。后台模式等待 /health，失败时显示日志。
PID、日志位于数据库所在目录，默认 agent_web/data/agent_web.pid 和 agent_web.log。
重复启动不会再启动已记录的后台 Web。

## 5. Ray、资源与执行

本地任务通过独立子进程调用 dj-process；Ray 任务通过 Ray Jobs 提交。
安装已包含 Ray CLI 和 Dashboard 依赖。Linux 同机默认尝试自动启动本地 Ray head。
手动启动方式：

```bash
.venv/bin/ray start --head --dashboard-host=127.0.0.1 --dashboard-port=8265
```

只用本地任务可设置 AGENT_WEB_RAY_AUTO_START=false。
Ray 不可用时本地任务仍可执行，Ray 任务等待可用环境。
/health 的 status=ok 表示 Web 可响应；另看 scheduler.ray_available 判断 Ray 可用性。

默认总并发 3、每用户并发 1。CPU 自动检测并保留两核；
GPU 通过 nvidia-smi 检测；均可在 .env 中调整。

默认支持同机 Ray。远程/多节点 worker 需要匹配的 Data-Juicer、Ray 和算子依赖，
并能通过相同绝对路径访问用户数据与输出，例如共享文件系统。
本应用不自动复制数据，也不安装远端 worker 依赖。Ray 端口仅供可信内部网络。

停止 Web 不会停止 Ray 和已提交任务。只有需要停止该机器的 Ray 时才执行
`.venv/bin/ray stop`，该命令会停止机器上的 Ray 进程。

## 6. 验证

仓库整理时已验证 Web 启动、登录、页面、静态资源和上传，Web 回归测试为 27 项通过。
补齐 CPU PyTorch 依赖后，全新 Linux 环境的完整数据处理与 Ray 执行尚未完成端到端验证。
真实模型对话也需要部署者提供有效 API Key 后验证。下列命令供部署者进行完整自检。

不需要模型 Key 的真实服务自检：

```bash
.venv/bin/python -m agent_web.scripts.smoke_test
```

自检使用临时数据库、随机端口，创建临时管理员并启动真实 HTTP 服务；
检查登录、页面、静态资源、JSONL 上传、队列、真实 Data-Juicer 文本长度过滤与结果下载。
它使用固定方案，不调用 LLM，不改生产数据，也不自动启动 Ray；完成后清理临时服务和数据。

已有同机 Ray head 时：

```bash
.venv/bin/python -m agent_web.scripts.smoke_test --executor ray --ray-address http://127.0.0.1:8265
```

回归测试：

```bash
uv sync --locked --extra web --group web-test --no-dev --python 3.12
.venv/bin/python -m pytest agent_web/tests -q
node --test agent_web/tests/markdown.test.js  # 需要 Node.js 22+
```

GitHub Actions 的 Web deployment checks 自动验证完整安装、Web 测试和本地执行自检，无需 Secrets。

有效模型 Key 配置后，还需手动验证：
登录 → 上传小型 JSONL → 选择文件 → 发送“保留文本长度至少 5 的记录” →
检查方案 → 提交任务 → 下载结果。这才验证真实供应商访问和 Agent 规划。
无 Key 的部署自检不能代替该步骤。当前外部数据集提供方未接入，使用上传文件和用户工作区。

## 7. 长期部署、升级与备份

后台脚本适合试用，不负责开机启动和崩溃重启。长期运行可配置单进程 systemd，
将下例路径、用户名替换为实际值：

```ini
[Unit]
Description=Data-Juicer Agent Web
After=network-online.target

[Service]
Type=simple
User=dja
WorkingDirectory=/opt/data-juicer-agents
Environment=PATH=/opt/data-juicer-agents/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/data-juicer-agents/.venv/bin/python -m agent_web.app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

服务用户必须有数据库、工作区和模型缓存的读写权限。使用一个 Web 进程；
当前 Agent 活动与恢复逻辑不支持额外配置 uvicorn --workers。
systemd 管理时通过 systemctl 启停；stop.sh 只管理脚本自己的 PID。

公网访问请配置 HTTPS 反向代理，并设置 AGENT_WEB_COOKIE_SECURE=true。
HTTP 测试保持 false，否则登录 Cookie 不会发送。代理应配置上传大小、请求时限及 SSE 流式响应。

升级前先等任务完成或取消，停止 Web，备份 .env、SQLite 数据库及工作区。
如果存在 -wal/-shm 文件也要一起备份。自定义路径需单独备份；
在线 SQLite 备份使用 backup API，不要在正在写入时仅复制主 db 文件。

```bash
bash agent_web/stop.sh
git pull --ff-only
bash agent_web/install.sh
bash agent_web/start.sh --background
bash agent_web/status.sh
```

安装脚本不清空账号、会话、任务或用户文件。

## 8. 上传 GitHub

提交源码、模板、静态资源、.env.example、pyproject.toml、uv.lock、脚本、文档和测试，
保留上游 LICENSE 和来源说明。不要提交 .venv、真实 .env、Key、数据库、用户文件、日志和缓存。
重复源码包 agent_web.zip 已从 Git 跟踪中移除，本地文件仍保留。
发布前检查 git status --short，使用 Git 推送，不要压缩含用户数据的整个目录上传。
