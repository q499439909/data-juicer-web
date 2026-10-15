# Deploy the Web UI

This repository includes the Web UI and its matching Agents backend.
Data-Juicer is installed from PyPI; no sibling source checkout is needed.
See the [Chinese guide](DEPLOY_ZH.md) for detailed configuration and operations.

## Install

Use Linux / WSL2, Git, Bash and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.12 is recommended; uv can download Python and create the virtual environment.
The front end uses local assets and needs no npm build.

```bash
git clone https://github.com/q499439909/data-juicer-web.git
cd data-juicer-web
bash agent_web/install.sh
# Edit agent_web/.env: set your DASHSCOPE_API_KEY and an available model.
bash agent_web/scripts/create_admin.sh admin
bash agent_web/start.sh --background
bash agent_web/status.sh
```

Open http://localhost:8090 or your server address. The admin password is prompted.
The installer uses uv.lock and installs the web extra, including Data-Juicer 1.5.5,
AgentScope 1.0.14 and Ray 2.57.0. CPU text tasks need no GPU; media/model operators
may require additional dependencies, model downloads or system libraries.
The default environment includes official CPU PyTorch 2.8.0 from download.pytorch.org,
so the first task does not trigger an unpinned CUDA dependency installation.
GPU model operators require a hardware-specific PyTorch/CUDA configuration.

The application loads agent_web/.env as dotenv data; explicit process environment variables
take precedence. AGENT_WEB_ENV_FILE selects another file. Keep credentials out of Git.
Use start.sh without --background for foreground mode; stop with Ctrl+C.
Use stop.sh to stop the script-managed background Web. Logs/PID files default to agent_web/data.

## Verify

```bash
.venv/bin/python -m agent_web.scripts.smoke_test
uv sync --locked --extra web --group web-test --no-dev --python 3.12
.venv/bin/python -m pytest agent_web/tests -q
node --test agent_web/tests/markdown.test.js  # Requires Node.js 22+
```

The smoke command starts a real HTTP server with a temporary database and random port.
It checks admin login, pages/assets, file upload, scheduling, a real Data-Juicer filter,
and downloading the filtered records. It uses a deterministic plan, calls no LLM,
does not start Ray and does not modify production data.

With an existing same-host Ray head, run:

```bash
.venv/bin/python -m agent_web.scripts.smoke_test --executor ray
```

To verify Agent chat, configure a valid model key, upload/select a small JSONL file,
ask the Agent to filter short texts, inspect the plan, execute it and download the output.
The external dataset provider is not configured; use uploaded workspace files.
GitHub Actions validates installation, Web tests and local execution without Secrets.

## Operations

Linux defaults to starting a localhost Ray head automatically. Set
AGENT_WEB_RAY_AUTO_START=false for local-only execution. /health reports Web readiness
and separate Ray availability. Remote workers need matching dependencies and shared data
paths; this application does not distribute user datasets to workers.

Background mode does not restart after a crash/reboot. Use a single-process systemd service
for persistent deployments; see the detailed guide. Do not configure multiple uvicorn workers.
Stopping Web does not stop Ray or submitted jobs.

For public access, configure HTTPS through a reverse proxy and set
AGENT_WEB_COOKIE_SECURE=true. Keep it false for HTTP tests.
Before upgrading, finish/cancel tasks, stop Web and back up .env, the database and workspace
files. Run git pull --ff-only, install.sh, start.sh and status.sh. Runtime data stays outside Git.
