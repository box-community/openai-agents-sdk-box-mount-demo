from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell, Skills
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.entries import BoxMount, DockerVolumeMountStrategy, LocalDir
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from docker import from_env as docker_from_env
from dotenv import load_dotenv
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout

from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent


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
    box_mount_config_file: str
    box_sub_type: str
    box_root_folder_id: str
    box_mount_subpath: str | None
    box_mount_dir: str
    box_access_token: str | None
    box_token: str | None
    box_impersonate: str | None
    box_owned_by: str | None
    box_plugin_config_dir: Path
    prompt_history_file: Path
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


def _sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return sanitized or "box-sandbox-cli"


def _stage_box_config_for_plugin(
    *,
    session_id: str,
    box_config_file: Path,
    plugin_config_dir: Path,
) -> str:
    plugin_config_dir.mkdir(parents=True, exist_ok=True)
    staged_name = f"{_sanitize_filename(session_id)}-box-config.json"
    staged_path = plugin_config_dir / staged_name
    staged_path.write_text(box_config_file.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(staged_path, 0o600)
    return f"/data/config/{staged_name}"


def _mint_box_token_payload(
    *,
    box_config: dict[str, Any],
    client_id: str,
    client_secret: str,
    box_sub_type: str,
    impersonate: str | None,
) -> dict[str, Any]:
    app_settings = box_config["boxAppSettings"]
    app_auth = app_settings["appAuth"]

    if box_sub_type == "enterprise":
        subject = box_config["enterpriseID"]
    else:
        if not impersonate:
            raise ValueError(
                "BOX_SUB_TYPE=user requires BOX_IMPERSONATE so a user access token can be minted."
            )
        subject = impersonate

    private_key = load_pem_private_key(
        app_auth["privateKey"].encode("utf-8"),
        password=app_auth["passphrase"].encode("utf-8"),
    )
    assertion = jwt.encode(
        {
            "iss": client_id,
            "sub": subject,
            "box_sub_type": box_sub_type,
            "aud": "https://api.box.com/oauth2/token",
            "jti": secrets.token_urlsafe(32),
            "exp": int(time.time()) + 45,
        },
        private_key,
        algorithm="RS512",
        headers={"kid": app_auth["publicKeyID"]},
    )

    request = Request(
        "https://api.box.com/oauth2/token",
        data=urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Box token exchange failed: HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Box token exchange failed: {exc.reason}") from exc

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Box token exchange succeeded but no access_token was returned.")

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        expiry_ts = time.time() + float(expires_in)
    else:
        expiry_ts = time.time() + 3600

    token_payload: dict[str, Any] = {
        "access_token": access_token,
        "token_type": payload.get("token_type", "bearer"),
        "expiry": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry_ts)),
    }
    refresh_token = payload.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        token_payload["refresh_token"] = refresh_token
    return token_payload


def _coerce_box_token(
    *,
    env_box_token: str | None,
    env_box_access_token: str | None,
    minted_token_payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    if env_box_token:
        return None, env_box_token

    if env_box_access_token:
        try:
            parsed = json.loads(env_box_access_token)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            access_token = parsed.get("access_token")
            if isinstance(access_token, str) and access_token:
                token_payload = {
                    "access_token": access_token,
                    "token_type": parsed.get("token_type", "bearer"),
                    "expiry": parsed.get(
                        "expiry",
                        time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
                        ),
                    ),
                }
                refresh_token = parsed.get("refresh_token")
                if isinstance(refresh_token, str) and refresh_token:
                    token_payload["refresh_token"] = refresh_token
                return None, json.dumps(token_payload, separators=(",", ":"))

        return env_box_access_token, None

    return None, json.dumps(minted_token_payload, separators=(",", ":"))


def _token_access_token(
    *,
    box_access_token: str | None,
    box_token: str | None,
) -> str:
    if box_access_token:
        return box_access_token
    if box_token:
        try:
            payload = json.loads(box_token)
        except json.JSONDecodeError as exc:
            raise ValueError("BOX_TOKEN must be valid JSON.") from exc
        access_token = payload.get("access_token")
        if isinstance(access_token, str) and access_token:
            return access_token
    raise ValueError("No usable Box access token is available.")


def load_settings() -> Settings:
    project_root = Path.cwd()
    env_file = _resolve_env_file(project_root)
    load_dotenv(env_file, override=False)

    box_config_file = _resolve_path(_require_env("BOX_CONFIG_FILE"), base_dir=env_file.parent)
    if not box_config_file.is_file():
        raise FileNotFoundError(f"BOX_CONFIG_FILE does not exist: {box_config_file}")

    session_db = _resolve_path(
        os.getenv("BOX_SESSION_DB", ".box-sandbox-cli/session.sqlite3"),
        base_dir=project_root,
    )
    session_db.parent.mkdir(parents=True, exist_ok=True)
    prompt_history_file = _resolve_path(
        os.getenv("BOX_PROMPT_HISTORY", ".box-sandbox-cli/prompt_history.txt"),
        base_dir=project_root,
    )
    prompt_history_file.parent.mkdir(parents=True, exist_ok=True)

    session_id = os.getenv("BOX_SESSION_ID", "box-sandbox-cli").strip() or "box-sandbox-cli"
    box_client_id = _require_env("BOX_CLIENT_ID")
    box_client_secret = _require_env("BOX_CLIENT_SECRET")
    box_sub_type = os.getenv("BOX_SUB_TYPE", "user").strip() or "user"
    box_impersonate = _optional_env("BOX_IMPERSONATE")
    box_plugin_config_dir = Path(
        os.getenv("BOX_PLUGIN_CONFIG_DIR", "/var/lib/docker-plugins/rclone/config")
    )

    box_config = json.loads(box_config_file.read_text(encoding="utf-8"))
    box_mount_config_file = _stage_box_config_for_plugin(
        session_id=session_id,
        box_config_file=box_config_file,
        plugin_config_dir=box_plugin_config_dir,
    )
    minted_token_payload = _mint_box_token_payload(
        box_config=box_config,
        client_id=box_client_id,
        client_secret=box_client_secret,
        box_sub_type=box_sub_type,
        impersonate=box_impersonate,
    )
    box_access_token, box_token = _coerce_box_token(
        env_box_token=_optional_env("BOX_TOKEN"),
        env_box_access_token=_optional_env("BOX_ACCESS_TOKEN"),
        minted_token_payload=minted_token_payload,
    )

    return Settings(
        env_file=env_file,
        project_root=project_root,
        openai_api_key=_require_env("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip(),
        sandbox_image=os.getenv("SANDBOX_IMAGE", DEFAULT_PYTHON_SANDBOX_IMAGE).strip(),
        box_client_id=box_client_id,
        box_client_secret=box_client_secret,
        box_config_file=box_config_file,
        box_mount_config_file=box_mount_config_file,
        box_sub_type=box_sub_type,
        box_root_folder_id=_require_env("BOX_ROOT_FOLDER_ID"),
        box_mount_subpath=_optional_env("BOX_MOUNT_SUBPATH"),
        box_mount_dir=os.getenv("BOX_MOUNT_DIR", "box").strip() or "box",
        box_access_token=box_access_token,
        box_token=box_token,
        box_impersonate=box_impersonate,
        box_owned_by=_optional_env("BOX_OWNED_BY"),
        box_plugin_config_dir=box_plugin_config_dir,
        prompt_history_file=prompt_history_file,
        session_id=session_id,
        session_db=session_db,
    )


def build_manifest(settings: Settings) -> Manifest:
    mount = BoxMount(
        path=settings.box_mount_subpath,
        client_id=settings.box_client_id,
        client_secret=settings.box_client_secret,
        access_token=settings.box_access_token,
        token=settings.box_token,
        box_config_file=settings.box_mount_config_file,
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
        capabilities=[
            Filesystem(),
            Shell(),
            Skills(from_=LocalDir(src="skills")),
        ],
    )


def _print_banner(settings: Settings) -> None:
    print("Box Sandbox CLI")
    print(f"Model: {settings.openai_model}")
    print(f"Sandbox image: {settings.sandbox_image}")
    print(f"Box mount: /workspace/{settings.box_mount_dir}")
    print("Commands: /exit, /quit, /clear, /help, /events")
    print()


def _print_help() -> None:
    print("Enter a normal prompt to talk to the SandboxAgent.")
    print("/events prints enterprise Box events from the last 24 hours.")
    print("/clear clears saved conversation history but keeps the live sandbox session running.")
    print("/exit or /quit ends the CLI and deletes the Docker sandbox.")


def _print_stream_item(event: RunItemStreamEvent) -> None:
    if event.name == "tool_called":
        print("\n[tool called]", flush=True)
    elif event.name == "tool_output":
        output = getattr(event.item, "output", None)
        print(f"\n[tool output: {output}]", flush=True)
    elif event.name == "handoff_requested":
        print("\n[handoff requested]", flush=True)
    elif event.name == "handoff_occured":
        print("\n[handoff completed]", flush=True)


async def _stream_agent_reply(
    agent: SandboxAgent,
    prompt: str,
    *,
    chat_session: SQLiteSession,
    run_config: RunConfig,
) -> str:
    result = Runner.run_streamed(
        agent,
        prompt,
        session=chat_session,
        run_config=run_config,
    )
    saw_text = False
    print("assistant> ", end="", flush=True)
    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                print(event.data.delta, end="", flush=True)
                saw_text = True
        elif isinstance(event, RunItemStreamEvent):
            _print_stream_item(event)
        elif isinstance(event, AgentUpdatedStreamEvent):
            print(f"\n[agent updated: {event.new_agent.name}]", flush=True)

    if result.run_loop_exception:
        raise result.run_loop_exception

    final_output = result.final_output
    if not saw_text and final_output is not None:
        print(final_output, end="", flush=True)

    print(flush=True)
    return "" if final_output is None else str(final_output)


async def run_cli() -> None:
    settings = load_settings()
    agent = build_agent(settings)
    chat_session = SQLiteSession(settings.session_id, str(settings.session_db))
    prompt_session = PromptSession[str](
        history=FileHistory(str(settings.prompt_history_file)),
        auto_suggest=AutoSuggestFromHistory(),
    )

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

            if settings.box_access_token:
                print(
                    "Warning: using BOX_ACCESS_TOKEN directly. For Box JWT flows, the CLI now "
                    "prefers an auto-minted BOX_TOKEN JSON blob from BOX_CONFIG_FILE."
                )
                print()

            with patch_stdout():
                while True:
                    try:
                        prompt = await prompt_session.prompt_async("user> ")
                    except EOFError:
                        print()
                        break
                    except KeyboardInterrupt:
                        print()
                        continue

                    prompt = prompt.strip()
                    if not prompt:
                        continue
                    if prompt in EXIT_COMMANDS:
                        break
                    if prompt == "/help":
                        _print_help()
                        continue
                    if prompt == "/events":
                        _print_events(settings)
                        continue
                    if prompt == "/clear":
                        await chat_session.clear_session()
                        print("assistant> Cleared conversation history.")
                        continue

                    await _stream_agent_reply(
                        agent,
                        prompt,
                        chat_session=chat_session,
                        run_config=run_config,
                    )
    finally:
        if sandbox is not None:
            await docker_client.delete(sandbox)


def _format_box_time(value: str | None) -> str:
    if not value:
        return "-"
    return value


def _format_event_actor(entry: dict[str, Any]) -> str:
    created_by = entry.get("created_by")
    if isinstance(created_by, dict):
        name = created_by.get("name")
        login = created_by.get("login")
        if isinstance(name, str) and name:
            if isinstance(login, str) and login:
                return f"{name} <{login}>"
            return name
    return "-"


def _format_event_source(entry: dict[str, Any]) -> str:
    source = entry.get("source")
    if not isinstance(source, dict):
        return "-"
    source_type = source.get("type")
    source_name = source.get("name")
    parts: list[str] = []
    if isinstance(source_type, str) and source_type:
        parts.append(source_type)
    if isinstance(source_name, str) and source_name:
        parts.append(source_name)
    return ": ".join(parts) if parts else "-"


def _markdown_cell(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _event_phrase(event_type: str | None) -> str:
    if not event_type:
        return "performed an event"

    phrases = {
        "COLLABORATION_INVITE": "invited a collaborator",
        "COLLABORATION_ACCEPT": "accepted a collaboration",
        "COLLABORATION_REMOVE": "removed a collaboration",
        "COLLABORATION_ROLE_CHANGE": "changed a collaboration role",
        "COMMENT_CREATE": "added a comment",
        "COMMENT_DELETE": "deleted a comment",
        "COPY": "copied an item",
        "DELETE": "deleted an item",
        "DOWNLOAD": "downloaded an item",
        "EDIT": "edited an item",
        "LOCK": "locked an item",
        "MOVE": "moved an item",
        "PREVIEW": "previewed an item",
        "RENAME": "renamed an item",
        "SHARE": "shared an item",
        "TASK_ASSIGNMENT_CREATE": "created a task assignment",
        "TASK_ASSIGNMENT_DELETE": "deleted a task assignment",
        "TASK_ASSIGNMENT_UPDATE": "updated a task assignment",
        "TASK_CREATE": "created a task",
        "TASK_UPDATE": "updated a task",
        "UNDELETE": "restored an item",
        "UNLOCK": "unlocked an item",
        "UNSHARE": "removed a share",
        "UPLOAD": "uploaded an item",
    }
    if event_type in phrases:
        return phrases[event_type]
    return event_type.replace("_", " ").lower()


def _format_event_description(entry: dict[str, Any]) -> str:
    actor = _format_event_actor(entry)
    source = entry.get("source")
    source_name = None
    source_type = None
    if isinstance(source, dict):
        raw_name = source.get("name")
        raw_type = source.get("type")
        if isinstance(raw_name, str) and raw_name:
            source_name = raw_name
        if isinstance(raw_type, str) and raw_type:
            source_type = raw_type.lower()

    subject = actor if actor != "-" else "Someone"
    phrase = _event_phrase(entry.get("event_type"))

    if source_name and source_type:
        return f"{subject} {phrase} on {source_type} '{source_name}'."
    if source_name:
        return f"{subject} {phrase} on '{source_name}'."
    return f"{subject} {phrase}."


def _get_json(url: str, *, access_token: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Box API request failed: HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Box API request failed: {exc.reason}") from exc


def _fetch_enterprise_events(settings: Settings, *, hours: int = 24) -> list[dict[str, Any]]:
    access_token = _token_access_token(
        box_access_token=settings.box_access_token,
        box_token=settings.box_token,
    )
    created_before = datetime.now(UTC)
    created_after = created_before - timedelta(hours=hours)

    params = {
        "stream_type": "admin_logs",
        "stream_position": "0",
        "limit": "500",
        "created_after": created_after.isoformat().replace("+00:00", "Z"),
        "created_before": created_before.isoformat().replace("+00:00", "Z"),
    }

    events: list[dict[str, Any]] = []
    next_stream_position: str | None = None

    while True:
        if next_stream_position is not None:
            params["stream_position"] = next_stream_position

        payload = _get_json(
            f"https://api.box.com/2.0/events?{urlencode(params)}",
            access_token=access_token,
        )
        entries = payload.get("entries", [])
        if isinstance(entries, list):
            events.extend(entry for entry in entries if isinstance(entry, dict))

        new_position = payload.get("next_stream_position")
        chunk_size = payload.get("chunk_size")
        if not isinstance(new_position, str) or not new_position or new_position == next_stream_position:
            break
        if not isinstance(chunk_size, int) or chunk_size == 0:
            break
        next_stream_position = new_position

    return events


def _print_events(settings: Settings) -> None:
    events = _fetch_enterprise_events(settings)
    print(f"Enterprise events from the last 24 hours: {len(events)}")
    print()

    if not events:
        print("No events found.")
        return

    print("| created_at | event_type | actor | source | description | event_id |")
    print("| --- | --- | --- | --- | --- | --- |")
    for entry in events:
        created_at = _markdown_cell(_format_box_time(entry.get("created_at")))
        event_type = _markdown_cell(entry.get("event_type"))
        actor = _markdown_cell(_format_event_actor(entry))
        source = _markdown_cell(_format_event_source(entry))
        description = _markdown_cell(_format_event_description(entry))
        event_id = _markdown_cell(entry.get("event_id"))
        print(
            f"| {created_at} | {event_type} | {actor} | {source} | "
            f"{description} | {event_id} |"
        )


def run_events_command() -> None:
    settings = load_settings()
    _print_events(settings)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="box-sandbox-cli")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("events", help="List enterprise Box events from the last 24 hours.")
    return parser.parse_args(argv)


def main() -> None:
    try:
        args = _parse_args(sys.argv[1:])
        if args.command == "events":
            run_events_command()
        else:
            asyncio.run(run_cli())
    except KeyboardInterrupt:
        print("\nExiting.")
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
