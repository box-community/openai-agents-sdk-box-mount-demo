from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.entries import BoxMount, DockerVolumeMountStrategy
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from docker import from_env as docker_from_env
from dotenv import load_dotenv


EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit"}


@dataclass(slots=True)
class Settings:
    env_file: Path
    project_root: Path
    openai_api_key: str
    openai_model: str
    sandbox_image: str
    box_client_id: str
    box_client_secret: str
    box_config_file: Path
    box_config_credentials: str
    box_sub_type: str
    box_root_folder_id: str
    box_mount_subpath: str | None
    box_mount_dir: str
    box_access_token: str | None
    box_token: str | None
    box_impersonate: str | None
    box_owned_by: str | None
    session_id: str
    session_db: Path


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_env_file(project_root: Path) -> Path:
    env_file = project_root / ".env.local"
    if not env_file.is_file():
        raise FileNotFoundError(f"Expected env file at {env_file}")
    return env_file


def _resolve_path(raw_path: str, *, base_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return candidate


def load_settings() -> Settings:
    project_root = Path.cwd()
    env_file = _resolve_env_file(project_root)
    load_dotenv(env_file, override=False)

    box_config_file = _resolve_path(_require_env("BOX_CONFIG_FILE"), base_dir=env_file.parent)
    if not box_config_file.is_file():
        raise FileNotFoundError(f"BOX_CONFIG_FILE does not exist: {box_config_file}")

    # Inline the JSON credentials so the Docker rclone volume driver does not need direct
    # filesystem access to the host-side config file path.
    box_config_credentials = json.dumps(json.loads(box_config_file.read_text(encoding="utf-8")))

    session_db = _resolve_path(
        os.getenv("BOX_SESSION_DB", ".box-sandbox-cli/session.sqlite3"),
        base_dir=project_root,
    )
    session_db.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        env_file=env_file,
        project_root=project_root,
        openai_api_key=_require_env("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip(),
        sandbox_image=os.getenv("SANDBOX_IMAGE", DEFAULT_PYTHON_SANDBOX_IMAGE).strip(),
        box_client_id=_require_env("BOX_CLIENT_ID"),
        box_client_secret=_require_env("BOX_CLIENT_SECRET"),
        box_config_file=box_config_file,
        box_config_credentials=box_config_credentials,
        box_sub_type=os.getenv("BOX_SUB_TYPE", "user").strip() or "user",
        box_root_folder_id=_require_env("BOX_ROOT_FOLDER_ID"),
        box_mount_subpath=_optional_env("BOX_MOUNT_SUBPATH"),
        box_mount_dir=os.getenv("BOX_MOUNT_DIR", "box").strip() or "box",
        box_access_token=_optional_env("BOX_ACCESS_TOKEN"),
        box_token=_optional_env("BOX_TOKEN"),
        box_impersonate=_optional_env("BOX_IMPERSONATE"),
        box_owned_by=_optional_env("BOX_OWNED_BY"),
        session_id=os.getenv("BOX_SESSION_ID", "box-sandbox-cli").strip() or "box-sandbox-cli",
        session_db=session_db,
    )


def build_manifest(settings: Settings) -> Manifest:
    mount = BoxMount(
        path=settings.box_mount_subpath,
        client_id=settings.box_client_id,
        client_secret=settings.box_client_secret,
        access_token=settings.box_access_token,
        token=settings.box_token,
        config_credentials=settings.box_config_credentials,
        box_sub_type=settings.box_sub_type,
        root_folder_id=settings.box_root_folder_id,
        impersonate=settings.box_impersonate,
        owned_by=settings.box_owned_by,
        read_only=False,
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
    )
    return Manifest(entries={settings.box_mount_dir: mount})


def build_agent(settings: Settings) -> SandboxAgent:
    mount_path = f"/workspace/{settings.box_mount_dir}"
    return SandboxAgent(
        name="Box Sandbox Assistant",
        model=settings.openai_model,
        instructions=(
            "You are a helpful sandboxed file assistant. "
            f"The Box mount is available at `{mount_path}`. "
            "Read and write files there when the user asks. "
            "Prefer inspecting the filesystem before making assumptions. "
            "When you change files, mention the exact paths you touched."
        ),
        default_manifest=build_manifest(settings),
        capabilities=[Filesystem(), Shell()],
    )


def _print_banner(settings: Settings) -> None:
    print("Box Sandbox CLI")
    print(f"Model: {settings.openai_model}")
    print(f"Sandbox image: {settings.sandbox_image}")
    print(f"Box mount: /workspace/{settings.box_mount_dir}")
    print("Commands: /exit, /quit, /clear, /help")
    print()


def _print_help() -> None:
    print("Enter a normal prompt to talk to the SandboxAgent.")
    print("/clear clears saved conversation history but keeps the live sandbox session running.")
    print("/exit or /quit ends the CLI and deletes the Docker sandbox.")


async def run_cli() -> None:
    settings = load_settings()
    agent = build_agent(settings)
    chat_session = SQLiteSession(settings.session_id, str(settings.session_db))

    docker_client = DockerSandboxClient(docker_from_env())
    run_config: RunConfig | None = None
    sandbox = None

    try:
        try:
            sandbox = await docker_client.create(
                manifest=agent.default_manifest,
                options=DockerSandboxClientOptions(image=settings.sandbox_image),
            )
        except Exception as exc:
            raise RuntimeError(
                "Failed to create the Docker sandbox. Make sure Docker is running and the "
                "rclone Docker volume plugin is installed for BoxMount."
            ) from exc

        async with sandbox:
            run_config = RunConfig(sandbox=SandboxRunConfig(session=sandbox))
            _print_banner(settings)

            if not settings.box_access_token and not settings.box_token:
                print(
                    "Warning: BOX_ACCESS_TOKEN / BOX_TOKEN are not set. "
                    "Current BoxMount docs note non-interactive mounts may require one."
                )
                print()

            while True:
                try:
                    prompt = await asyncio.to_thread(input, "user> ")
                except EOFError:
                    print()
                    break

                prompt = prompt.strip()
                if not prompt:
                    continue
                if prompt in EXIT_COMMANDS:
                    break
                if prompt == "/help":
                    _print_help()
                    continue
                if prompt == "/clear":
                    await chat_session.clear_session()
                    print("assistant> Cleared conversation history.")
                    continue

                result = await Runner.run(
                    agent,
                    prompt,
                    session=chat_session,
                    run_config=run_config,
                )
                print(f"assistant> {result.final_output}")
    finally:
        if sandbox is not None:
            await docker_client.delete(sandbox)


def main() -> None:
    try:
        asyncio.run(run_cli())
    except KeyboardInterrupt:
        print("\nExiting.")
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
