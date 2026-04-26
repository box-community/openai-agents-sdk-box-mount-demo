# Box Sandbox CLI

Simple interactive Python CLI built on [`openai-agents`](https://pypi.org/project/openai-agents/) that:

- starts a Docker-backed `SandboxAgent`
- mounts a Box folder read/write with `BoxMount`
- keeps the same live sandbox session across multiple CLI prompts

## What it expects

Create a `.env.local` file in this project with:

```dotenv
OPENAI_API_KEY=sk-...
BOX_CLIENT_ID=...
BOX_CLIENT_SECRET=...
BOX_CONFIG_FILE=/absolute/path/to/box-config.json
BOX_SUB_TYPE=user
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
BOX_MOUNT_DIR=box
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

Or:

```bash
python -m box_sandbox_cli.cli
```

## Notes

- The Box mount is created with `BoxMount(..., read_only=False)` so the agent can write back to Box.
- This CLI uses `DockerVolumeMountStrategy(driver="rclone")`, which means Docker needs to be running and the `rclone` Docker volume plugin must be available on the machine.
- The CLI reads `BOX_CONFIG_FILE`, inlines its JSON into `config_credentials`, and passes that to `BoxMount`. That avoids relying on the Docker volume plugin being able to see an arbitrary host path.
- The current SDK docs note that non-interactive Box mounts may require a minted `token` or `access_token`. The CLI accepts optional `BOX_TOKEN` and `BOX_ACCESS_TOKEN` overrides for that reason.
