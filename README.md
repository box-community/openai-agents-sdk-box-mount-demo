# OpenAI Agents SDK Box Mount demo

Simple interactive Python CLI that uses a agent to manage team and task information that is stored in Box.


- built on [`OpenAI Agents SDK`](https://pypi.org/project/openai-agents/)
- starts a Docker-backed `SandboxAgent`
- mounts a Box folder read/write with `BoxMount`
- keeps the same live sandbox session across multiple CLI prompts
- uses `Prompt Toolkit` for the interactive prompt
- streams assistant responses live as the `SandboxAgent` generates them


## Getting started

- Signup for a free [Box developer account](https://account.box.com/signup/developer)
- Create a new Platform App of type Server Auth - JWT
- Generate a public/private key pair for the JWT and download the JSON
- `git clone git@github.com:box-community/openai-agents-sdk-box-mount-demo.git`

## Setup

Create a `.env.local` file in this project with:

```dotenv
OPENAI_API_KEY=sk-...
BOX_CLIENT_ID=...
BOX_CLIENT_SECRET=...
BOX_CONFIG_FILE=./box-config.json
BOX_SUB_TYPE=enterprise
BOX_ROOT_FOLDER_ID=123456789
```

Optional environment variables:

```dotenv
OPENAI_MODEL=gpt-5.5
SANDBOX_IMAGE=python:3.14-slim
BOX_MOUNT_SUBPATH=
BOX_ACCESS_TOKEN=
BOX_TOKEN=
BOX_IMPERSONATE=
BOX_OWNED_BY=
BOX_PLUGIN_CONFIG_DIR=/var/lib/docker-plugins/rclone/config
BOX_MOUNT_DIR=box
BOX_PROMPT_HISTORY=.box-sandbox-cli/prompt_history.txt
BOX_SESSION_ID=box-sandbox-cli
BOX_SESSION_DB=.box-sandbox-cli/session.sqlite3
```

## Install

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e .
```

## Run

```bash
box-sandbox-cli
```

List enterprise Box events from the last 24 hours:

```bash
box-sandbox-cli events
```

Some simple prompts:

`Show me a status report for the team`

`Let's add a new team member`

`Mark task X as complete`

## Notes

- The Box mount is created with `BoxMount(..., read_only=False)` so the agent can write back to Box.
- This CLI uses `DockerVolumeMountStrategy(driver="rclone")`, which means Docker needs to be running and the `rclone` Docker volume plugin must be available on the machine.
- The CLI stages `BOX_CONFIG_FILE` into the Docker plugin config directory and passes that staged file to `BoxMount` as `box_config_file`.
- The CLI mints a fresh Box token payload from the JWT config on startup, then passes it to `BoxMount` as `token`. This keeps the mount inline while matching the rclone mode that works reliably in non-interactive Docker mounts.
- The `events` command calls `GET /events` with `stream_type=admin_logs` and a 24-hour window. Per Box's API docs, this requires admin privileges and the app scope `manage enterprise properties`.
- `BOX_TOKEN` is still supported as a manual override if you want to provide the JSON blob yourself.
- `BOX_ACCESS_TOKEN` is still supported, but only as a compatibility fallback; the auto-minted `BOX_TOKEN` path is preferred.
- If you use `BOX_SUB_TYPE=user`, also set `BOX_IMPERSONATE` to the Box user ID whose token should be minted.
- Prompt history is stored in `BOX_PROMPT_HISTORY`.
