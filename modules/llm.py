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
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CLI_PATH = os.environ.get("CLAUDE_CLI") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
WORK_DIR = os.path.expanduser("~/.cache/kab-llm")
TIMEOUT_S = 240
# Одновременно не больше 2 процессов CLI (память NUC, лимиты подписки)
_SLOTS = threading.BoundedSemaphore(2)


class LLMError(RuntimeError):
    """Ошибка вызова Claude через подписку."""


class LLMLimitError(LLMError):
    """Исчерпан лимит подписки (5-часовой / недельный, 429). reset_at — когда снимется
    (datetime с tz) или None, если CLI не сообщил время."""

    def __init__(self, raw: str, reset_at: "datetime | None" = None) -> None:
        self.raw = raw
        self.reset_at = reset_at
        super().__init__(f"Лимит подписки Claude исчерпан — LLM недоступен {self.until_text()}")

    def until_text(self) -> str:
        if not self.reset_at:
            return "до сброса лимита (время сброса неизвестно)"
        local = self.reset_at.astimezone(ZoneInfo("Europe/Berlin"))
        now = datetime.now(ZoneInfo("Europe/Berlin"))
        fmt = "%H:%M" if local.date() == now.date() else "%d.%m %H:%M"
        return f"до {local.strftime(fmt)} (Берлин)"


# «usage limit reached|1760000000», «5-hour limit reached ∙ resets 3pm», «weekly limit … resets Oct 22, 4am»
_LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|weekly limit|session limit|"
                       r"out of (extra )?usage|too many requests)", re.IGNORECASE)
_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_RESETS_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+)?"
                        r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?"
                        r"(?:\s*\((?P<tz>[^)]+)\))?", re.IGNORECASE)


def _parse_reset(text: str) -> "datetime | None":
    """Время сброса лимита из текста ошибки CLI (эпоха после «|» или «resets 3pm (Europe/Berlin)»)."""
    m = _EPOCH_RE.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    m = _RESETS_RE.search(text)
    if not m:
        return None
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else ZoneInfo("Europe/Berlin")
    except Exception:  # noqa: BLE001 — неизвестная зона в тексте
        tz = ZoneInfo("Europe/Berlin")
    hour = int(m.group("h")) % 12 if m.group("ampm") else int(m.group("h"))
    if (m.group("ampm") or "").lower() == "pm":
        hour += 12
    if hour > 23:
        return None
    now = datetime.now(tz)
    cand = now.replace(hour=hour, minute=int(m.group("m") or 0), second=0, microsecond=0)
    if m.group("mon"):
        try:
            month = datetime.strptime(m.group("mon"), "%b").month
            cand = cand.replace(month=month, day=int(m.group("day")))
            if cand < now - timedelta(days=1):
                cand = cand.replace(year=cand.year + 1)
        except ValueError:
            return None
    elif cand <= now:
        cand += timedelta(days=1)
    return cand


def _raise_if_limit(text: str, status: Any = None) -> None:
    if str(status) == "429" or _LIMIT_RE.search(text or ""):
        raise LLMLimitError((text or "")[:300], _parse_reset(text or ""))


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
        _raise_if_limit(err)
        raise LLMError(f"Claude CLI завершился без результата (код {proc.returncode}): {err}")
    if result.get("is_error"):
        _raise_if_limit(str(result.get("result") or ""), result.get("api_error_status"))
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
