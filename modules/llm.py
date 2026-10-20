# LLM через подписку Claude (Claude Code CLI, `claude -p`), без Anthropic API ключа.
#
# Совместимая замена `anthropic.Anthropic(...).messages.create(...)`:
#   client = llm.Client()
#   response = client.messages.create(model=..., max_tokens=..., system=..., messages=[...],
#                                     output_config={"effort": ..., "format": {"type": "json_schema", "schema": ...}},
#                                     tools=[{"type": "web_search_..."}], thinking={"type": "disabled"})
#   response.content — блоки {type: "text" | "server_tool_use", text}, response.usage — токены.
#
# Авторизация — вход claude.ai у пользователя сервиса (`claude auth login`, ~/.claude/.credentials.json).
# ANTHROPIC_API_KEY из окружения дочернего процесса убирается, чтобы CLI не ушёл на платный API.
# Запуск в пустой папке и с --setting-sources "" — без CLAUDE.md проекта и пользовательских настроек.

import json
import logging
import os
import shutil
import subprocess
import threading
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

CLI_PATH = os.environ.get("CLAUDE_CLI") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
WORK_DIR = os.path.expanduser("~/.cache/kab-llm")
TIMEOUT_S = 240
# Одновременно не больше 2 процессов CLI (память NUC, лимиты подписки)
_SLOTS = threading.BoundedSemaphore(2)


class LLMError(RuntimeError):
    """Ошибка вызова Claude через подписку."""


def _env(max_tokens: int | None, thinking_off: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update({
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    })
    if max_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    if thinking_off:
        env["MAX_THINKING_TOKENS"] = "0"
    return env


def _prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    """Склеить messages в один текст для stdin (в проекте всегда один user-message)."""
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
        if len(messages) > 1:
            parts.append(f"[{m.get('role', 'user')}]\n{c}")
        else:
            parts.append(str(c or ""))
    return "\n\n".join(parts)


def _system_text(system: Any) -> str:
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system or ""


def run(*, model: str, messages: list[dict[str, Any]], system: Any = None, max_tokens: int | None = None,
        output_config: dict | None = None, tools: list[dict] | None = None, thinking: dict | None = None,
        **_ignored: Any) -> SimpleNamespace:
    schema = None
    effort = None
    if output_config:
        effort = output_config.get("effort")
        fmt = output_config.get("format") or {}
        if fmt.get("type") == "json_schema":
            schema = fmt.get("schema")
    web = any("web_search" in (t.get("type", "") + t.get("name", "")) for t in (tools or []))

    cmd = [
        CLI_PATH, "-p",
        "--output-format", "stream-json", "--verbose",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--model", model,
        "--system-prompt", _system_text(system),
        "--tools", "WebSearch" if web else "",
    ]
    if web:
        cmd += ["--allowedTools", "WebSearch"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if effort:
        cmd += ["--effort", effort]

    thinking_off = not thinking or thinking.get("type") == "disabled"
    os.makedirs(WORK_DIR, exist_ok=True)
    with _SLOTS:
        try:
            proc = subprocess.run(
                cmd, input=_prompt_from_messages(messages), capture_output=True, text=True,
                timeout=TIMEOUT_S, cwd=WORK_DIR, env=_env(max_tokens, thinking_off),
            )
        except FileNotFoundError as e:
            raise LLMError(f"Не найден Claude Code CLI ({CLI_PATH})") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"Claude не ответил за {TIMEOUT_S} с") from e

    blocks: list[SimpleNamespace] = []
    result: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use" and b.get("name") == "WebSearch":
                    blocks.append(SimpleNamespace(type="server_tool_use", name="web_search", text=""))
        elif ev.get("type") == "result":
            result = ev

    if result is None:
        err = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise LLMError(f"Claude CLI завершился без результата (код {proc.returncode}): {err}")
    if result.get("is_error"):
        raise LLMError(f"Claude вернул ошибку: {result.get('subtype')} {str(result.get('result'))[:300]}"
                       f" {result.get('api_error_status') or ''}".strip())

    if schema:
        so = result.get("structured_output")
        text = json.dumps(so, ensure_ascii=False) if so is not None else (result.get("result") or "")
    else:
        text = result.get("result") or ""
    blocks.append(SimpleNamespace(type="text", text=text))

    u = result.get("usage") or {}
    usage = SimpleNamespace(
        input_tokens=u.get("input_tokens", 0) or 0,
        output_tokens=u.get("output_tokens", 0) or 0,
        cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=u.get("cache_read_input_tokens", 0) or 0,
        subscription=True,
    )
    logger.info("LLM %s: %d мс, вход %d, выход %d токенов%s", model, result.get("duration_ms", 0),
                usage.input_tokens, usage.output_tokens, ", web_search" if len(blocks) > 1 else "")
    return SimpleNamespace(content=blocks, usage=usage, model=model, stop_reason=result.get("stop_reason"))


class _Messages:
    def create(self, **kwargs: Any) -> SimpleNamespace:
        return run(**kwargs)


class Client:
    """Замена anthropic.Anthropic: client.messages.create(...)."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.messages = _Messages()


def status() -> tuple[bool, str]:
    """Вошёл ли CLI в подписку claude.ai (для настроек и проверок)."""
    try:
        p = subprocess.run([CLI_PATH, "auth", "status"], capture_output=True, text=True, timeout=30,
                           env=_env(None, False), cwd=os.path.expanduser("~"))
        d = json.loads(p.stdout or "{}")
    except FileNotFoundError:
        return False, f"Не найден Claude Code CLI ({CLI_PATH})"
    except Exception as e:  # noqa: BLE001
        return False, f"Ошибка проверки: {e}"
    if d.get("loggedIn") and d.get("authMethod") == "claude.ai":
        return True, f"Подписка Claude подключена ({d.get('email') or d.get('subscriptionType') or 'claude.ai'})"
    if d.get("loggedIn"):
        return False, f"CLI вошёл не через подписку ({d.get('authMethod')}) — нужен claude.ai"
    return False, "CLI не авторизован: выполните `claude auth login` под пользователем сервиса"
