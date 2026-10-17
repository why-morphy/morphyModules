# ⟡ why-morphy — Shizu modules
# AIManager for Shizu
# Codex CLI and Claude Code manager with live usage and reset clocks.

""" Owner-only management for local AI coding CLIs plus 5-hour reset clocks.

    The manager supports subscription sign-in only: ChatGPT device login for Codex
    and Claude's browser login flow for Claude Code.  It never stores API keys."""

import asyncio
import contextlib
import html
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib import error as urlerror
from urllib import request as urlrequest

from pyrogram import Client, enums, types
from pyrogram.errors import MessageNotModified

from .. import loader, utils

try:
    from ..telegram.rich import button_rows
except ImportError:
    button_rows = None


AUTH_TIMEOUT = 10 * 60
REASON_LIMIT = 200
BAR_WIDTH = 10
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_BETA = "oauth-2025-04-20"
CLAUDE_USAGE_CACHE_SECONDS = 45
CLAUDE_RATE_LIMIT_FALLBACK_SECONDS = 120
CLAUDE_RATE_LIMIT_MAX_SECONDS = 3600
FIVE_HOUR_SECONDS = 5 * 60 * 60
URL_RE = re.compile(r"https:" r"//\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
CODE_RE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b")
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)?")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def premium(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


OK = premium("5260416304224936047", "✅")
ERROR = premium("5807626765874499116", "🚫")
WARN = premium("5364241851500997604", "⚠️")
LINK = premium("5780722455077196625", "🔗")
CLOCK = premium("5017179932451668652", "🕖")
SPARK = premium("4999015678238262018", "✨")
INFO = premium("5787544344906959608", "ℹ️")
BOLT = premium("5220226955206467824", "⚡️")
SHIELD = premium("5280506417478903827", "🛡")
CROWN = premium("5467406098367521267", "👑")
LOADING = premium("5978846612087114958", "🔁")
DOT = premium("5267444331010074275", "▫️")


@dataclass(frozen=True)
class Tool:
    name: str
    emoji: str
    executable: str
    installer_url: str
    installer_shell: str
    installer_command: str
    auth_args: tuple[str, ...]
    paths: tuple[str, ...]
    supports_logout: bool = False


@dataclass
class AuthSession:
    """A live CLI login, kept only in memory until it ends."""

    process: asyncio.subprocess.Process
    chat_id: int | None
    output: bytearray
    render: Callable[[str, bool], Awaitable[None]]
    inline: bool = False
    task: asyncio.Task | None = None
    url: str | None = None
    code: str | None = None


TOOLS = {
    "codex": Tool(
        "Codex CLI",
        premium("5283089397925851009", "🤖"),
        "codex",
        "https://chatgpt.com/codex/install.sh",
        "sh",
        "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        ("login", "--device-auth"),
        ("~/.local/bin/codex", "~/.npm-global/bin/codex", "~/.local/share/codex/bin/codex"),
        True,
    ),
    "claude": Tool(
        "Claude Code",
        premium("5285122515184689647", "🤖"),
        "claude",
        "https://claude.ai/install.sh",
        "bash",
        "curl -fsSL https://claude.ai/install.sh | bash",
        ("auth", "login"),
        ("~/.local/bin/claude", "~/.npm-global/bin/claude"),
    ),
}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.999))
    parts = []
    for suffix, unit in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, seconds = divmod(seconds, unit)
        if amount:
            parts.append(f"{amount}{suffix}")
        if len(parts) == 2:
            break
    return " ".join(parts) or "0s"


def progress_bar(percent: float) -> str:
    filled = max(0, min(BAR_WIDTH, round(percent / 100 * BAR_WIDTH)))
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


@loader.module(name="AIManager", author="why-morphy", version=2.1)
class AIManagerMod(loader.Module):
    """Manage Codex/Claude subscriptions, live usage, and 5-hour reset clocks."""

    strings = {
        "usage": (
            SPARK + " <b>AI Manager</b>\n\n"
            "<code>.ai</code> — menu with live usage and buttons\n"
            "<code>.ai codex status|limits|auth|install confirm|update confirm|logout confirm</code>\n"
            "<code>.ai claude status|limits|auth|install confirm|update confirm|input &lt;code&gt;</code>\n"
            "<code>.ai reset set &lt;codex|claude&gt; &lt;now|HH:MM|YYYY-MM-DDTHH:MM&gt;</code>\n"
            "<code>.ai reset [codex|claude]</code> · <code>.ai reset clear &lt;codex|claude&gt;</code>\n\n"
            "<code>.ai time [set &lt;HH:MM|YYYY-MM-DDTHH:MM&gt;|clear]</code>\n\n"
            + SHIELD + " Install and sign-in work only in Saved Messages or a private chat. "
            "Logins are stored by the provider CLIs, never by Shizu."
        ),
        "private_only": SHIELD + " For account safety, Codex and Claude controls work only in Saved Messages or a private chat.",
        "not_installed": "{emoji} <b>{tool}</b>\n" + DOT + " Not installed",
        "install_hint": "\n\nSend <code>.ai {provider} install</code> to install it.",
        "signed_out": DOT + " Not signed in",
        "signed_in": OK + " Signed in",
        "confirm_install": (
            WARN + " <b>Install {tool}?</b>\n\n"
            "This downloads and runs the official installer from <code>{host}</code> as the Shizu service user."
        ),
        "confirm_update": WARN + " <b>Update {tool}?</b>\n\nThe CLI will download and install its latest release.",
        "confirm_logout": WARN + " <b>Log out of Codex?</b>\n\nThe saved ChatGPT login for the Shizu service user will be removed.",
        "confirm_hint": "\n\nSend <code>.ai {provider} {action} confirm</code> to continue.",
        "busy": "⏳ Another install or update is already running.",
        "installing": LOADING + " Installing <b>{tool}</b>…",
        "updating": LOADING + " Updating <b>{tool}</b>…",
        "installed": OK + " <b>{tool}</b> installed{version}",
        "updated": OK + " <b>{tool}</b> updated{version}",
        "logged_out": OK + " Logged out of <b>Codex CLI</b>",
        "action_failed": ERROR + " <b>{tool}</b>: {action} failed\n<i>{reason}</i>",
        "usage_failed": ERROR + " Couldn't read usage\n<i>{reason}</i>",
        "usage_expired": ERROR + " Login expired — sign in again",
        "usage_empty": DOT + " No active usage windows",
        "usage_cached": INFO + " Showing the last Claude reading from <b>{age}</b> ago; live refresh in <b>{wait}</b>.",
        "usage_rate_limited": WARN + " Claude is temporarily limiting usage checks. Try again in <b>{wait}</b>.",
        "reset_usage": (
            CLOCK + " <b>5-hour reset clock</b>\n\n"
            "<code>.ai reset set &lt;codex|claude&gt; &lt;now|HH:MM|YYYY-MM-DDTHH:MM&gt;</code>\n"
            "<code>.ai reset [codex|claude]</code>\n"
            "<code>.ai reset clear &lt;codex|claude&gt;</code>\n"
            "<code>.ai time set &lt;HH:MM|YYYY-MM-DDTHH:MM&gt;</code>\n\n"
            + INFO + "The start time anchors a repeating 5-hour countdown. It does not estimate or restrict provider usage."
        ),
        "reset_title": CLOCK + " <b>Manual 5-hour reset clocks</b>",
        "reset_empty": DOT + " No manual reset clocks are set.",
        "reset_hint": INFO + " Set the start when the provider's 5-hour window began. The clock repeats every 5 hours.",
        "reset_missing": DOT + " No manual 5-hour clock is set for <b>{tool}</b>.",
        "reset_scheduled": CLOCK + " <b>{tool} 5-hour clock</b>\n{state}",
        "reset_pending": "Starts in <b>{countdown}</b> · {at}",
        "reset_next": "Next reset in <b>{countdown}</b> · {at}",
        "reset_set": OK + " Set <b>{tool}</b>'s 5-hour reset start.\n\n{status}",
        "reset_cleared": OK + " Cleared <b>{tool}</b>'s manual 5-hour reset clock.",
        "reset_bad_provider": ERROR + " Choose <code>codex</code> or <code>claude</code>.",
        "reset_bad_start": ERROR + " Start must be <code>now</code>, <code>HH:MM</code>, or <code>YYYY-MM-DDTHH:MM</code>.",
        "time_title": "🕰 <b>Clock calibration</b>",
        "time_server": "Server clock: <code>{time}</code> ({zone})",
        "time_user_unset": "Your clock: <i>not calibrated</i>",
        "time_user": "Your clock: <code>{time}</code>",
        "time_match": OK + " Server and your entered time match.",
        "time_difference": WARN + " Your entered time differs by <b>{difference}</b>.",
        "time_hint": INFO + " Telegram does not reveal your device time zone. Enter your current time once; it is used only to display and enter reset times.",
        "time_set": OK + " Your clock was calibrated. Displayed times now use <code>{time}</code>.",
        "time_cleared": OK + " Clock calibration cleared; reset times use the server clock.",
        "time_bad": ERROR + " Time must be <code>HH:MM</code> or <code>YYYY-MM-DDTHH:MM</code>.",
        "time_usage": (
            "<code>.ai time</code> — show server and calibrated clocks\n"
            "<code>.ai time set &lt;HH:MM|YYYY-MM-DDTHH:MM&gt;</code>\n"
            "<code>.ai time clear</code>"
        ),
        "plan": CROWN + " Plan: <b>{plan}</b>",
        "window": BOLT + " <b>{label}</b>  {bar}  <b>{remaining}%</b> left",
        "window_reset": CLOCK + " Resets in <b>{countdown}</b> · {at}",
        "auth_running": LOADING + " Starting <b>{tool}</b> sign-in…",
        "auth_title": "{emoji} <b>{tool}</b> · sign-in",
        "auth_code": SHIELD + " One-time code: <code>{code}</code>",
        "auth_link": LINK + ' <a href="{url}">Open sign-in page</a>',
        "auth_waiting_link": LOADING + " Waiting for the sign-in link…",
        "auth_codex_hint": INFO + " Open the page and enter the code. This message updates once you're signed in.",
        "auth_claude_hint": (
            INFO + " Sign in on the page. If Claude shows a code, send it with "
            "<code>.ai claude input &lt;code&gt;</code>. It goes only to the running CLI and is never saved."
        ),
        "auth_claude_hint_inline": (
            INFO + " Sign in on the page. If Claude shows a code, press <b>Send code</b> and paste it "
            "after the bot name. It goes only to the running CLI and is never saved."
        ),
        "auth_done": OK + " Signed in to <b>{tool}</b>",
        "auth_failed": ERROR + " <b>{tool}</b> sign-in didn't complete\n<i>{reason}</i>",
        "auth_timeout": "Timed out after 10 minutes.",
        "auth_stopping": "⛔ Stopping the sign-in…",
        "auth_stopped": "⛔ <b>{tool}</b> sign-in stopped",
        "already_auth": LOADING + " A <b>{tool}</b> sign-in is already running.",
        "no_auth": ERROR + " No <b>{tool}</b> sign-in is waiting for a code. Start one with <code>.ai {provider} auth</code>.",
        "auth_wrong_chat": ERROR + " Send the code in the same private chat that started this sign-in.",
        "auth_input_missing": ERROR + " Add the code after <code>.ai claude input</code>.",
        "auth_input_sent": OK + " Code sent — waiting for Claude to confirm…",
        "menu": SPARK + " <b>AI Manager</b>",
        "menu_private_hint": SHIELD + " Codex and Claude controls appear only in Saved Messages or a private chat.",
        "btn_close": "✖️ Close",
        "btn_back": "◀️ Back",
        "btn_refresh": "🔄 Refresh",
        "btn_install": "📥 Install",
        "btn_update": "⬆️ Update",
        "btn_auth": "🔐 Sign in",
        "btn_relogin": "🔐 Sign in again",
        "btn_logout": "🚪 Log out",
        "btn_confirm": "✅ Confirm",
        "btn_cancel": "✖️ Cancel",
        "btn_open_link": "🔗 Open sign-in page",
        "btn_send_code": "🔑 Send code",
        "btn_stop_auth": "⛔ Stop sign-in",
        "btn_reset_clocks": "🕖 Reset clocks",
        "btn_set_codex_reset": "🕖 Set Codex start",
        "btn_set_claude_reset": "🕖 Set Claude start",
        "btn_clear_codex_reset": "🗑 Clear Codex clock",
        "btn_clear_claude_reset": "🗑 Clear Claude clock",
        "btn_set_time": "🕰 Set your current time",
        "btn_clear_time": "↩️ Use server clock",
        "input_code": "Paste the code from Claude",
        "input_reset_start": "now, HH:MM or YYYY-MM-DDTHH:MM",
        "input_current_time": "HH:MM or YYYY-MM-DDTHH:MM",
        "close_failed": "Could not close this menu.",
        "cfg_menu_ttl": "How long AI Manager menu buttons remain active, in seconds.",
        "cfg_menu_ttl_label": "Menu button lifetime",
        "cfg_usage_timeout": "Maximum wait for a provider's live usage response, in seconds.",
        "cfg_usage_timeout_label": "Usage request timeout",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "menu_ttl",
                600,
                lambda: self.strings("cfg_menu_ttl"),
                validator=loader.validators.Integer(minimum=60, maximum=3600),
                label=lambda: self.strings("cfg_menu_ttl_label"),
            ),
            loader.ConfigValue(
                "usage_timeout",
                15,
                lambda: self.strings("cfg_usage_timeout"),
                validator=loader.validators.Integer(minimum=5, maximum=60),
                label=lambda: self.strings("cfg_usage_timeout_label"),
            ),
        )
        self._install_lock = asyncio.Lock()
        self._auth_sessions: dict[str, AuthSession] = {}
        self._claude_usage_lock = asyncio.Lock()
        self._claude_usage_cache: tuple[float, dict] | None = None
        self._claude_retry_after = 0.0

    async def on_unload(self):
        """Terminate temporary sign-in CLI processes before this module is replaced."""
        sessions = tuple(self._auth_sessions.values())
        self._auth_sessions.clear()
        for session in sessions:
            await self._stop_process(session.process)
        tasks = [session.task for session in sessions if session.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --------------------------------------------------------- shared CLI tools

    @staticmethod
    def _clean(data: bytes | str) -> str:
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        return ANSI_RE.sub("", data).replace("\r", "\n")

    def _reason(self, data: bytes | str) -> str:
        lines = [line.strip() for line in URL_RE.sub("", self._clean(data)).splitlines() if line.strip()]
        return utils.escape_html((lines[-1] if lines else "No details.")[:REASON_LIMIT])

    @staticmethod
    def _is_private(message: types.Message) -> bool:
        return getattr(getattr(message, "chat", None), "type", None) == enums.ChatType.PRIVATE

    @staticmethod
    def _tool_path(tool: Tool) -> str | None:
        if path := shutil.which(tool.executable):
            return path
        home = os.path.expanduser("~")
        for candidate in tool.paths:
            path = candidate.replace("~", home, 1)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    async def _run(self, *command: str, timeout: int = 120) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            return 127, str(error)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            output, _ = await proc.communicate()
            return 124, output.decode(errors="replace") + f"\nTimed out after {timeout}s."
        return proc.returncode or 0, output.decode(errors="replace")

    async def _version(self, path: str) -> str | None:
        code, output = await self._run(path, "--version", timeout=30)
        match = VERSION_RE.search(self._clean(output)) if code == 0 else None
        return match.group(0) if match else None

    async def _install_tool(self, tool: Tool) -> tuple[int, str]:
        if not shutil.which("curl"):
            return 127, "curl is not installed"
        if not shutil.which(tool.installer_shell):
            return 127, f"{tool.installer_shell} is not installed"
        # Both values are module constants; the owner confirmation makes this
        # deliberate remote-code installation explicit.
        return await self._run(tool.installer_shell, "-c", tool.installer_command, timeout=300)

    def _failed(self, tool: Tool, action: str, output: bytes | str) -> str:
        return self.strings("action_failed").format(tool=tool.name, action=action, reason=self._reason(output))

    async def _version_suffix(self, path: str) -> str:
        version = await self._version(path)
        return f" · <code>v{utils.escape_html(version)}</code>" if version else ""

    async def _install_text(self, tool: Tool) -> str:
        async with self._install_lock:
            code, output = await self._install_tool(tool)
        path = self._tool_path(tool)
        if code or not path:
            return self._failed(tool, "install", output)
        return self.strings("installed").format(tool=tool.name, version=await self._version_suffix(path))

    async def _update_text(self, tool: Tool, path: str) -> str:
        async with self._install_lock:
            code, output = await self._run(path, "update", timeout=300)
        if code:
            return self._failed(tool, "update", output)
        return self.strings("updated").format(tool=tool.name, version=await self._version_suffix(path))

    async def _logout_text(self, path: str) -> str:
        code, output = await self._run(path, "logout", timeout=30)
        if code:
            return self._failed(TOOLS["codex"], "logout", output)
        return self.strings("logged_out")

    async def _signed_in(self, provider: str, path: str) -> bool:
        if provider == "claude":
            return self._claude_oauth_token()[0] is not None
        code, _ = await self._run(path, "login", "status", timeout=30)
        return code == 0

    async def _provider_card(self, provider: str, detailed: bool = False) -> tuple[str, bool | None]:
        """Title, version and live usage for one provider; the flag is None when not installed."""
        tool = TOOLS[provider]
        path = self._tool_path(tool)
        reset_clock = self._five_hour_reset_status(provider)
        if not path:
            text = self.strings("not_installed").format(emoji=tool.emoji, tool=tool.name)
            return (f"{text}\n\n{reset_clock}" if reset_clock else text), None
        version, signed = await asyncio.gather(
            self._version_suffix(path) if detailed else asyncio.sleep(0, ""),
            self._signed_in(provider, path),
        )
        head = f"{tool.emoji} <b>{tool.name}</b>{version}"
        if not signed:
            text = f"{head}\n{self.strings('signed_out')}"
            return (f"{text}\n\n{reset_clock}" if reset_clock else text), False
        usage = await (self._codex_usage(path) if provider == "codex" else self._claude_usage())
        text = f"{head}\n{usage}"
        return (f"{text}\n\n{reset_clock}" if reset_clock else text), True

    def _auth_card(self, provider: str, session: AuthSession, note: str = "") -> str:
        tool = TOOLS[provider]
        details = []
        if session.code:
            details.append(self.strings("auth_code").format(code=html.escape(session.code)))
        if session.url:
            details.append(self.strings("auth_link").format(url=html.escape(session.url)))
        else:
            details.append(self.strings("auth_waiting_link"))
        if provider == "claude":
            hint = self.strings("auth_claude_hint_inline" if session.inline else "auth_claude_hint")
        else:
            hint = self.strings("auth_codex_hint")
        parts = [self.strings("auth_title").format(emoji=tool.emoji, tool=tool.name), "\n".join(details), hint]
        if note:
            parts.append(note)
        return "\n\n".join(parts)

    async def _watch_authorization(
        self,
        provider: str,
        tool: Tool,
        session: AuthSession,
    ) -> None:
        """Follow login output until the child exits, while leaving stdin available."""
        proc, output, shown = session.process, session.output, None
        deadline = time.monotonic() + AUTH_TIMEOUT
        try:
            while proc.returncode is None and time.monotonic() < deadline:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    await proc.wait()
                    break
                output.extend(line)
                raw = self._clean(bytes(output))
                if urls := URL_RE.findall(raw):
                    session.url = urls[-1]
                if codes := CODE_RE.findall(URL_RE.sub(" ", raw)):
                    session.code = codes[-1]
                state = (session.url, session.code)
                if state != shown and any(state):
                    shown = state
                    await session.render(self._auth_card(provider, session), True)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
                text = self.strings("auth_failed").format(tool=tool.name, reason=self.strings("auth_timeout"))
            elif proc.returncode == 0:
                if provider == "claude":
                    self._claude_usage_cache = None
                    self._claude_retry_after = 0.0
                text = self.strings("auth_done").format(tool=tool.name)
            else:
                text = self.strings("auth_failed").format(tool=tool.name, reason=self._reason(bytes(output)))
            await session.render(text, False)
        finally:
            if self._auth_sessions.get(provider) is session:
                self._auth_sessions.pop(provider, None)

    async def _start_auth(
        self,
        provider: str,
        path: str,
        chat_id: int | None,
        render: Callable[[str, bool], Awaitable[None]],
        inline: bool = False,
    ) -> AuthSession | str:
        tool = TOOLS[provider]
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                *tool.auth_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            return self.strings("auth_failed").format(tool=tool.name, reason=self._reason(str(error)))
        session = AuthSession(proc, chat_id, bytearray(), render, inline)
        self._auth_sessions[provider] = session
        session.task = utils.spawn(self._watch_authorization(provider, tool, session))
        return session

    async def _authorize(self, provider: str, message: types.Message, tool: Tool, path: str) -> None:
        if provider in self._auth_sessions:
            return await utils.answer(message, self.strings("already_auth").format(tool=tool.name))
        await utils.answer(message, self.strings("auth_running").format(tool=tool.name))

        async def render(text: str, running: bool) -> None:
            await utils.answer(message, text)

        session = await self._start_auth(provider, path, message.chat.id, render)
        if isinstance(session, str):
            return await utils.answer(message, session)
        if not session.url:
            await render(self._auth_card(provider, session), True)

    async def _write_auth_input(self, provider: str, value: str, chat_id: int | None = None) -> str:
        session = self._auth_sessions.get(provider)
        tool = TOOLS[provider]
        if session is None or session.process.returncode is not None:
            self._auth_sessions.pop(provider, None)
            return self.strings("no_auth").format(tool=tool.name, provider=provider)
        if chat_id is not None and session.chat_id is not None and session.chat_id != chat_id:
            return self.strings("auth_wrong_chat")
        if not value:
            return self.strings("auth_input_missing")
        try:
            session.process.stdin.write(value.encode() + b"\n")
            await session.process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as error:
            return self.strings("auth_failed").format(tool=tool.name, reason=self._reason(str(error)))
        return self.strings("auth_input_sent")

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            await proc.wait()
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def _app_server_request(
        self,
        proc: asyncio.subprocess.Process,
        request_id: int,
        method: str,
        params: dict | None = None,
        timeout: int = 15,
    ) -> dict:
        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        proc.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await proc.stdin.drain()
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("Codex app-server closed before replying.")
            try:
                response = json.loads(line)
            except (TypeError, ValueError):
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                message = response["error"].get("message", "Unknown app-server error")
                raise RuntimeError(str(message))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex returned an invalid rate-limit response.")
            return result
        raise TimeoutError("Codex app-server did not reply in time.")

    def _clock_offset_seconds(self) -> float:
        """Return the owner's display-clock offset relative to the server."""
        try:
            offset = float(self.get("user_clock_offset_seconds", 0))
        except (TypeError, ValueError):
            return 0.0
        # Real-world time-zone differences cannot exceed 26 hours. Keeping the
        # stored value in this range protects displayed dates from bad input.
        return max(-26 * 3600, min(26 * 3600, offset))

    def _clock_is_calibrated(self) -> bool:
        return bool(self.get("user_clock_calibrated", False))

    def _display_moment(self, timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp).astimezone() + timedelta(
            seconds=self._clock_offset_seconds()
        )

    def _short_time(self, timestamp: float) -> str:
        moment = self._display_moment(timestamp)
        if moment.date() == self._display_moment(time.time()).date():
            return moment.strftime("%H:%M")
        return moment.strftime("%a %d %b, %H:%M")

    def _window_status(self, label: str, window: dict) -> str:
        try:
            remaining = max(0, min(100, 100 - int(window["usedPercent"])))
        except (KeyError, TypeError, ValueError):
            remaining = 0
        text = self.strings("window").format(label=label, bar=progress_bar(remaining), remaining=remaining)
        reset_at = window.get("resetsAt")
        if isinstance(reset_at, (int, float)):
            if reset_at > 20_000_000_000:
                reset_at /= 1000
            countdown = format_duration(reset_at - time.time())
            text += "\n" + self.strings("window_reset").format(countdown=countdown, at=self._short_time(reset_at))
        return text

    def _usage_text(self, plan: object, windows: list[tuple[str, dict]]) -> str:
        if not windows:
            return self.strings("usage_empty")
        parts = [self.strings("plan").format(plan=utils.escape_html(str(plan)))] if plan else []
        parts.extend(self._window_status(label, window) for label, window in windows)
        return "\n".join(parts)

    async def _codex_usage(self, path: str) -> str:
        """Read the authenticated account's live limits through Codex app-server."""
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            return self.strings("usage_failed").format(reason=self._reason(str(error)))
        try:
            await self._app_server_request(
                proc,
                1,
                "initialize",
                {"clientInfo": {"name": "AIManager", "version": "1.0"}},
                timeout=self.config["usage_timeout"],
            )
            proc.stdin.write(b'{"method":"initialized"}\n')
            await proc.stdin.drain()
            result = await self._app_server_request(
                proc,
                2,
                "account/rateLimits/read",
                {"excludeResetCreditDetails": True},
                timeout=self.config["usage_timeout"],
            )
        except (OSError, RuntimeError, TimeoutError, asyncio.TimeoutError) as error:
            return self.strings("usage_failed").format(reason=self._reason(str(error) or "No reply from Codex."))
        finally:
            await self._stop_process(proc)

        snapshots = result.get("rateLimitsByLimitId")
        snapshot = snapshots.get("codex") if isinstance(snapshots, dict) else None
        if not isinstance(snapshot, dict):
            snapshot = result.get("rateLimits")
        if not isinstance(snapshot, dict):
            return self.strings("usage_failed").format(reason="Codex returned no usage for this account.")
        windows = [window for window in (snapshot.get("primary"), snapshot.get("secondary")) if isinstance(window, dict)]
        labelled = []
        for window in sorted(windows, key=lambda item: item.get("windowDurationMins") or 0):
            duration = window.get("windowDurationMins")
            label = "5-hour" if duration == 300 else "Weekly" if duration == 10_080 else format_duration((duration or 0) * 60)
            labelled.append((label, window))
        return self._usage_text(snapshot.get("planType"), labelled)

    @staticmethod
    def _claude_oauth_token() -> tuple[str | None, dict]:
        """Read Claude Code's locally saved login without exposing its secret."""
        credentials_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
        try:
            with open(credentials_path, encoding="utf-8") as credentials_file:
                credentials = json.load(credentials_file)
        except (OSError, TypeError, ValueError):
            return None, {}
        oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
        if not isinstance(oauth, dict):
            return None, {}
        token = oauth.get("accessToken")
        return (token, oauth) if isinstance(token, str) and token else (None, oauth)

    @staticmethod
    def _claude_usage_request(token: str, timeout: int) -> dict:
        """Ask Anthropic only for the signed-in Claude Code account's usage data."""
        request = urlrequest.Request(
            CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": CLAUDE_USAGE_BETA,
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlrequest.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(65_536))
        if not isinstance(result, dict):
            raise ValueError("Claude returned an invalid usage response.")
        return result

    @staticmethod
    def _claude_timestamp(value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value / 1000 if value > 20_000_000_000 else value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return None

    @classmethod
    def _claude_usage_window(cls, value: object) -> dict | None:
        if not isinstance(value, dict):
            return None
        utilization = value.get("utilization")
        if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
            return None
        used_percent = float(utilization) * 100 if utilization <= 1 else float(utilization)
        return {
            "usedPercent": round(max(0, min(100, used_percent))),
            "resetsAt": cls._claude_timestamp(value.get("resets_at")),
        }

    def _claude_usage_text(self, account: dict, usage: dict) -> str:
        windows = [
            (label, window)
            for label, key in (("5-hour", "five_hour"), ("Weekly", "seven_day"))
            if (window := self._claude_usage_window(usage.get(key)))
        ]
        return self._usage_text(
            account.get("subscriptionType") or account.get("rateLimitTier"), windows
        )

    @staticmethod
    def _claude_retry_seconds(error: urlerror.HTTPError) -> int:
        try:
            seconds = int(error.headers.get("Retry-After", ""))
        except (AttributeError, TypeError, ValueError):
            seconds = CLAUDE_RATE_LIMIT_FALLBACK_SECONDS
        return min(CLAUDE_RATE_LIMIT_MAX_SECONDS, max(15, seconds))

    def _cached_claude_usage(self, account: dict, now: float, *, rate_limited: bool) -> str | None:
        if self._claude_usage_cache is None:
            return None
        checked_at, usage = self._claude_usage_cache
        age = max(0, now - checked_at)
        if age > CLAUDE_RATE_LIMIT_MAX_SECONDS:
            return None
        if rate_limited:
            note = self.strings("usage_rate_limited").format(
                wait=format_duration(self._claude_retry_after - now)
            )
        else:
            note = self.strings("usage_cached").format(
                age=format_duration(age),
                wait=format_duration(CLAUDE_USAGE_CACHE_SECONDS - age),
            )
        return f"{self._claude_usage_text(account, usage)}\n{note}"

    async def _claude_usage(self) -> str:
        """Read the live 5-hour and weekly Claude Code allowance without a CLI session."""
        token, account = self._claude_oauth_token()
        if token is None:
            return self.strings("signed_out")
        async with self._claude_usage_lock:
            now = time.monotonic()
            if now < self._claude_retry_after:
                cached = self._cached_claude_usage(account, now, rate_limited=True)
                return cached or self.strings("usage_rate_limited").format(
                    wait=format_duration(self._claude_retry_after - now)
                )
            cached = self._cached_claude_usage(account, now, rate_limited=False)
            if cached is not None and self._claude_usage_cache:
                checked_at, _ = self._claude_usage_cache
                if now - checked_at < CLAUDE_USAGE_CACHE_SECONDS:
                    return cached
            try:
                usage = await utils.run_sync(
                    self._claude_usage_request, token, self.config["usage_timeout"]
                )
            except urlerror.HTTPError as error:
                if error.code in {401, 403}:
                    return self.strings("usage_expired")
                if error.code == 429:
                    retry_now = time.monotonic()
                    self._claude_retry_after = retry_now + self._claude_retry_seconds(error)
                    cached = self._cached_claude_usage(account, retry_now, rate_limited=True)
                    return cached or self.strings("usage_rate_limited").format(
                        wait=format_duration(self._claude_retry_after - retry_now)
                    )
                return self.strings("usage_failed").format(
                    reason=f"Anthropic returned HTTP {error.code}. Try again shortly."
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return self.strings("usage_failed").format(
                    reason="Couldn't reach Claude's usage service. Try again shortly."
                )
            self._claude_usage_cache = (time.monotonic(), usage)
            self._claude_retry_after = 0.0
            return self._claude_usage_text(account, usage)

    async def _answer_card(self, message: types.Message, text: str):
        try:
            return await utils.answer(message, text)
        except MessageNotModified:
            # Shizu can receive its own edited message as a duplicate update.
            # The visible result is already correct, so there is nothing to do.
            return message

    async def _tool_command(self, provider: str, args: list[str], message: types.Message, auth_input: str = ""):
        tool = TOOLS[provider]
        action = args[0] if args else "status"
        confirmed = len(args) == 2 and args[1] == "confirm"
        if not self._is_private(message):
            return await utils.answer(message, self.strings("private_only"))
        not_installed = (
            self.strings("not_installed").format(emoji=tool.emoji, tool=tool.name)
            + self.strings("install_hint").format(provider=provider)
        )
        if action in {"status", "limits"} and len(args) <= 1:
            text, installed = await self._provider_card(provider, detailed=action == "status")
            return await self._answer_card(message, not_installed if installed is None else text)
        if action == "install":
            if not confirmed:
                return await utils.answer(message, self._confirm_text(provider, action) + self.strings("confirm_hint").format(provider=provider, action=action))
            await utils.answer(message, self.strings("installing").format(tool=tool.name))
            return await utils.answer(message, await self._install_text(tool))
        if provider == "claude" and action == "input":
            return await utils.answer(message, await self._write_auth_input(provider, auth_input, message.chat.id))
        if action not in {"update", "auth", "logout"}:
            return await utils.answer(message, self.strings("usage"))
        path = self._tool_path(tool)
        if not path:
            return await utils.answer(message, not_installed)
        if action == "update":
            if not confirmed:
                return await utils.answer(message, self._confirm_text(provider, action) + self.strings("confirm_hint").format(provider=provider, action=action))
            await utils.answer(message, self.strings("updating").format(tool=tool.name))
            return await utils.answer(message, await self._update_text(tool, path))
        if action == "auth" and len(args) == 1:
            return await self._authorize(provider, message, tool, path)
        if action == "logout" and tool.supports_logout:
            if not confirmed:
                return await utils.answer(message, self._confirm_text(provider, action) + self.strings("confirm_hint").format(provider=provider, action=action))
            return await utils.answer(message, await self._logout_text(path))
        await utils.answer(message, self.strings("usage"))

    def _confirm_text(self, provider: str, action: str) -> str:
        tool = TOOLS[provider]
        if action == "install":
            return self.strings("confirm_install").format(tool=tool.name, host=tool.installer_url.split("/")[2])
        if action == "update":
            return self.strings("confirm_update").format(tool=tool.name)
        return self.strings("confirm_logout")

    # ---------------------------------------------------------- reset schedule

    @staticmethod
    def _parse_wall_time(value: str, now: float) -> float | None:
        """Parse an entered local clock time on the server's calendar."""
        value = value.strip()
        server_now = datetime.fromtimestamp(now).astimezone()
        if match := CLOCK_RE.fullmatch(value):
            hour, minute = map(int, match.groups())
            if hour > 23 or minute > 59:
                return None
            candidates = [
                server_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                + timedelta(days=day)
                for day in (-1, 0, 1)
            ]
            return min(candidates, key=lambda candidate: abs(candidate.timestamp() - now)).timestamp()
        for pattern in (
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, pattern).replace(
                    tzinfo=server_now.tzinfo
                ).timestamp()
            except ValueError:
                pass
        return None

    def _set_user_clock(self, value: str) -> str:
        now = time.time()
        wall_timestamp = self._parse_wall_time(value, now)
        if wall_timestamp is None:
            return self.strings("time_bad")
        offset = wall_timestamp - now
        self.set("user_clock_offset_seconds", offset)
        self.set("user_clock_calibrated", True)
        return self.strings("time_set").format(
            time=self._display_moment(now).strftime("%Y-%m-%d %H:%M")
        )

    def _clear_user_clock(self) -> str:
        self.set("user_clock_offset_seconds", 0)
        self.set("user_clock_calibrated", False)
        return self.strings("time_cleared")

    def _clock_status(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        server_now = datetime.fromtimestamp(now).astimezone()
        lines = [
            self.strings("time_title"),
            self.strings("time_server").format(
                time=server_now.strftime("%Y-%m-%d %H:%M"),
                zone=server_now.tzname() or "server local time",
            ),
        ]
        offset = self._clock_offset_seconds()
        if not self._clock_is_calibrated():
            lines.extend([
                self.strings("time_user_unset"),
                self.strings("time_hint"),
            ])
            return "\n".join(lines)
        lines.append(
            self.strings("time_user").format(
                time=self._display_moment(now).strftime("%Y-%m-%d %H:%M")
            )
        )
        if abs(offset) < 60:
            lines.append(self.strings("time_match"))
        else:
            direction = "ahead of" if offset > 0 else "behind"
            lines.append(
                self.strings("time_difference").format(
                    difference=f"{format_duration(abs(offset))} {direction} server"
                )
            )
        lines.append(self.strings("time_hint"))
        return "\n".join(lines)

    def _parse_start(self, value: str, now: float) -> float | None:
        value = value.strip().lower()
        if value == "now":
            return now
        wall_timestamp = self._parse_wall_time(value, now)
        if wall_timestamp is None:
            return None
        # Input is in the calibrated display clock. Translate it back to the
        # server's absolute clock before storing the repeating schedule.
        start = wall_timestamp - self._clock_offset_seconds()
        if CLOCK_RE.fullmatch(value) and start <= now:
            start += timedelta(days=1).total_seconds()
        return start

    def _reset_starts(self) -> dict[str, float]:
        raw = self.get("five_hour_reset_starts", {})
        if not isinstance(raw, dict):
            return {}
        starts = {}
        for provider, start in raw.items():
            if provider not in TOOLS:
                continue
            try:
                starts[provider] = float(start)
            except (TypeError, ValueError):
                continue
        return starts

    def _five_hour_reset_status(self, provider: str, now: float | None = None) -> str | None:
        start = self._reset_starts().get(provider)
        if start is None:
            return None
        now = time.time() if now is None else now
        tool = TOOLS[provider]
        if now < start:
            state = self.strings("reset_pending").format(
                countdown=format_duration(start - now), at=self._short_time(start)
            )
        else:
            periods = int((now - start) // FIVE_HOUR_SECONDS) + 1
            reset_at = start + periods * FIVE_HOUR_SECONDS
            state = self.strings("reset_next").format(
                countdown=format_duration(reset_at - now), at=self._short_time(reset_at)
            )
        return self.strings("reset_scheduled").format(tool=tool.name, state=state)

    def _reset_command_text(self, args: list[str]) -> str:
        starts, now = self._reset_starts(), time.time()
        if not args:
            statuses = [
                self._five_hour_reset_status(provider, now)
                for provider in TOOLS
                if provider in starts
            ]
            return self.strings("reset_title") + "\n\n" + self._clock_status(now) + "\n\n" + (
                "\n\n".join(statuses) if statuses else self.strings("reset_empty")
            )
        action = args[0].lower()
        if action in {"help", "?"}:
            return self.strings("reset_usage")
        if action == "set" and len(args) == 3:
            provider = args[1].lower()
            if provider not in TOOLS:
                return self.strings("reset_bad_provider")
            start = self._parse_start(args[2], now)
            if start is None:
                return self.strings("reset_bad_start")
            starts[provider] = start
            self.set("five_hour_reset_starts", starts)
            return self.strings("reset_set").format(
                tool=TOOLS[provider].name,
                status=self._five_hour_reset_status(provider, now),
            )
        if action == "clear" and len(args) == 2:
            provider = args[1].lower()
            if provider not in TOOLS:
                return self.strings("reset_bad_provider")
            if provider not in starts:
                return self.strings("reset_missing").format(tool=TOOLS[provider].name)
            del starts[provider]
            self.set("five_hour_reset_starts", starts)
            return self.strings("reset_cleared").format(tool=TOOLS[provider].name)
        if len(args) == 1 and action in TOOLS:
            return self._five_hour_reset_status(action, now) or self.strings(
                "reset_missing"
            ).format(tool=TOOLS[action].name)
        return self.strings("reset_usage")

    # ------------------------------------------------------------- inline menu

    def _btn(self, key: str, callback, *args, style: str | None = None) -> dict:
        button = {"text": self.strings(key), "callback": callback, "args": args}
        if style:
            button["style"] = style
        return button

    def _input(
        self, key: str, prompt: str, handler, *args, style: str | None = None
    ) -> dict:
        button = {
            "text": self.strings(key),
            "input": self.strings(prompt),
            "handler": handler,
            "args": args,
        }
        if style:
            button["style"] = style
        return button

    @staticmethod
    def _rich_form(text: str, markup: list) -> dict | None:
        """Build a structured rich view while retaining the text as a fallback."""
        if button_rows is None:
            return None
        sections = [section for section in text.split("\n\n") if section.strip()]
        if not sections:
            return {"html": button_rows(markup)}
        title, *cards = sections
        html = f"<h2>{title}</h2>" + "".join(
            f"<blockquote>{card.replace(chr(10), '<br>')}</blockquote>"
            for card in cards
        )
        return {"html": html + button_rows(markup)}

    @classmethod
    async def _show(cls, call, cid: str, text: str, markup: list) -> None:
        rich = cls._rich_form(text, markup)
        extra = {"rich_message": rich} if rich else {}
        await call.edit(text, reply_markup=markup, inline_message_id=cid, **extra)

    async def _main_view(self, chat: int | None) -> tuple[str, list]:
        rows = []
        parts = [self.strings("menu")]
        if chat is None:
            parts.append(self.strings("menu_private_hint"))
        else:
            cards = await asyncio.gather(*(self._provider_card(provider) for provider in TOOLS))
            parts.extend(text for text, _ in cards)
            rows.append([
                {
                    "text": TOOLS[provider].name,
                    "callback": self._menu_provider,
                    "args": (provider, chat),
                    "style": "primary",
                }
                for provider in TOOLS
            ])
        rows.append([
            self._btn("btn_reset_clocks", self._menu_reset, chat, style="primary")
        ])
        rows.append([
            self._btn("btn_refresh", self._menu_main, chat, style="primary"),
            self._btn("btn_close", self._menu_close, style="danger"),
        ])
        return "\n\n".join(parts), rows

    async def _menu_main(self, call, chat: int | None) -> None:
        await self._show(call, call.inline_message_id, *await self._main_view(chat))

    async def _menu_close(self, call) -> None:
        if not await call.delete():
            return await call.answer(self.strings("close_failed"), show_alert=True)
        await call.answer()

    async def _reset_view(
        self, chat: int | None, note: str = ""
    ) -> tuple[str, list]:
        starts = self._reset_starts()
        schedules = [
            self._five_hour_reset_status(provider)
            for provider in TOOLS
            if provider in starts
        ]
        parts = [self.strings("reset_title"), self._clock_status()]
        parts.append("\n\n".join(schedules) if schedules else self.strings("reset_empty"))
        parts.append(self.strings("reset_hint"))
        if note:
            parts.insert(0, note)

        rows = [[
            self._input(
                "btn_set_codex_reset",
                "input_reset_start",
                self._input_reset_start,
                "codex",
                chat,
                style="success",
            ),
            self._input(
                "btn_set_claude_reset",
                "input_reset_start",
                self._input_reset_start,
                "claude",
                chat,
                style="success",
            ),
        ]]
        clear_rows = []
        if "codex" in starts:
            clear_rows.append(
                self._btn("btn_clear_codex_reset", self._menu_clear_reset, "codex", chat, style="danger")
            )
        if "claude" in starts:
            clear_rows.append(
                self._btn("btn_clear_claude_reset", self._menu_clear_reset, "claude", chat, style="danger")
            )
        if clear_rows:
            rows.append(clear_rows)
        rows.append([
            self._input(
                "btn_set_time",
                "input_current_time",
                self._input_current_time,
                chat,
                style="primary",
            )
        ])
        if self._clock_is_calibrated():
            rows.append([
                self._btn("btn_clear_time", self._menu_clear_time, chat, style="danger")
            ])
        rows.append([
            self._btn("btn_refresh", self._menu_reset, chat, style="primary"),
            self._btn("btn_back", self._menu_main, chat, style="primary"),
        ])
        return "\n\n".join(parts), rows

    async def _menu_reset(self, call, chat: int | None) -> None:
        await self._show(call, call.inline_message_id, *await self._reset_view(chat))

    async def _input_reset_start(
        self, call, query: str, provider: str, chat: int | None
    ) -> None:
        note = self._reset_command_text(["set", provider, query.strip()])
        await self._show(call, call.inline_message_id, *await self._reset_view(chat, note))

    async def _menu_clear_reset(
        self, call, provider: str, chat: int | None
    ) -> None:
        note = self._reset_command_text(["clear", provider])
        await self._show(call, call.inline_message_id, *await self._reset_view(chat, note))

    async def _input_current_time(self, call, query: str, chat: int | None) -> None:
        note = self._set_user_clock(query)
        await self._show(call, call.inline_message_id, *await self._reset_view(chat, note))

    async def _menu_clear_time(self, call, chat: int | None) -> None:
        await self._show(
            call,
            call.inline_message_id,
            *await self._reset_view(chat, self._clear_user_clock()),
        )

    async def _provider_view(self, provider: str, chat: int, note: str = "") -> tuple[str, list]:
        tool = TOOLS[provider]
        text, signed = await self._provider_card(provider, detailed=True)
        if note:
            text = f"{note}\n\n{text}"
        rows = []
        if signed is None:
            rows.append([self._btn("btn_install", self._menu_confirm, provider, "install", chat, style="success")])
        else:
            rows.append([
                self._btn(
                    "btn_relogin" if signed else "btn_auth",
                    self._menu_auth,
                    provider,
                    chat,
                    style="primary" if signed else "success",
                ),
                self._btn(
                    "btn_update",
                    self._menu_confirm,
                    provider,
                    "update",
                    chat,
                    style="primary",
                ),
            ])
            if signed and tool.supports_logout:
                rows.append([self._btn("btn_logout", self._menu_confirm, provider, "logout", chat, style="danger")])
        rows.append([
            self._btn("btn_refresh", self._menu_provider, provider, chat, style="primary"),
            self._btn("btn_back", self._menu_main, chat, style="primary"),
        ])
        return text, rows

    async def _menu_provider(self, call, provider: str, chat: int) -> None:
        await self._show(call, call.inline_message_id, *await self._provider_view(provider, chat))

    async def _menu_confirm(self, call, provider: str, action: str, chat: int) -> None:
        rows = [[
            self._btn("btn_confirm", self._menu_run, provider, action, chat, style="success"),
            self._btn("btn_cancel", self._menu_provider, provider, chat, style="primary"),
        ]]
        await self._show(call, call.inline_message_id, self._confirm_text(provider, action), rows)

    async def _menu_run(self, call, provider: str, action: str, chat: int) -> None:
        tool, cid = TOOLS[provider], call.inline_message_id
        path = self._tool_path(tool)
        if action != "install" and not path:
            return await self._show(call, cid, *await self._provider_view(provider, chat))
        if action == "logout":
            return await self._show(call, cid, *await self._provider_view(provider, chat, await self._logout_text(path)))
        if self._install_lock.locked():
            return await call.answer(self.strings("busy"), show_alert=True)
        progress = "installing" if action == "install" else "updating"
        await self._show(call, cid, self.strings(progress).format(tool=tool.name), [])
        note = await (self._install_text(tool) if action == "install" else self._update_text(tool, path))
        await self._show(call, cid, *await self._provider_view(provider, chat, note))

    def _auth_rows(self, provider: str, chat: int, cid: str) -> list:
        rows = []
        session = self._auth_sessions.get(provider)
        if session and session.url:
            rows.append([{"text": self.strings("btn_open_link"), "url": session.url}])
        if provider == "claude":
            rows.append([
                self._input(
                    "btn_send_code",
                    "input_code",
                    self._input_auth,
                    provider,
                    chat,
                    cid,
                    style="success",
                )
            ])
        rows.append([self._btn("btn_stop_auth", self._menu_auth_stop, provider, chat, style="danger")])
        return rows

    async def _menu_auth(self, call, provider: str, chat: int) -> None:
        tool, cid = TOOLS[provider], call.inline_message_id
        path = self._tool_path(tool)
        if not path:
            return await self._show(call, cid, *await self._provider_view(provider, chat))
        if session := self._auth_sessions.get(provider):
            return await self._show(call, cid, self._auth_card(provider, session), self._auth_rows(provider, chat, cid))

        async def render(text: str, running: bool) -> None:
            with contextlib.suppress(Exception):
                if running:
                    await self._show(call, cid, text, self._auth_rows(provider, chat, cid))
                else:
                    await self._show(call, cid, *await self._provider_view(provider, chat, text))

        await self._show(call, cid, self.strings("auth_running").format(tool=tool.name), [])
        session = await self._start_auth(provider, path, chat, render, inline=True)
        if isinstance(session, str):
            return await self._show(call, cid, *await self._provider_view(provider, chat, session))
        if not session.url:
            await render(self._auth_card(provider, session), True)

    async def _menu_auth_stop(self, call, provider: str, chat: int) -> None:
        session, cid = self._auth_sessions.get(provider), call.inline_message_id
        if session is None:
            return await self._menu_provider(call, provider, chat)
        await call.answer(self.strings("auth_stopping"))
        await self._stop_process(session.process)
        if session.task:
            with contextlib.suppress(Exception):
                await session.task
        note = self.strings("auth_stopped").format(tool=TOOLS[provider].name)
        await self._show(call, cid, *await self._provider_view(provider, chat, note))

    async def _input_auth(self, call, query: str, provider: str, chat: int, cid: str) -> None:
        result = await self._write_auth_input(provider, query.strip())
        if session := self._auth_sessions.get(provider):
            return await self._show(call, cid, self._auth_card(provider, session, result), self._auth_rows(provider, chat, cid))
        await self._show(call, cid, *await self._provider_view(provider, chat, result))

    @loader.command()
    @loader.owner
    async def ai(self, app: Client, message: types.Message):
        """AI manager: live usage, provider controls, and 5-hour reset clocks"""
        raw = utils.get_args_raw(message).strip()
        tokens = raw.split()
        if not tokens:
            chat = message.chat.id if self._is_private(message) else None
            text, markup = await self._main_view(chat)
            if rich := self._rich_form(text, markup):
                return await message.answer(
                    text,
                    reply_markup=markup,
                    rich_message=rich,
                    ttl=self.config["menu_ttl"],
                )
            return await self.inline.form(
                text=text,
                message=message,
                reply_markup=markup,
                ttl=self.config["menu_ttl"],
            )
        if tokens[0].lower() in {"help", "?"}:
            return await utils.answer(message, self.strings("usage"))
        area = tokens[0].lower()
        if area in TOOLS:
            auth_input = ""
            if len(tokens) > 1 and tokens[1].lower() == "input":
                parts = raw.split(None, 2)
                auth_input = parts[2] if len(parts) == 3 else ""
            return await self._tool_command(area, [item.lower() for item in tokens[1:]], message, auth_input)
        if area in {"reset", "resets"}:
            return await utils.answer(message, self._reset_command_text(tokens[1:]))
        if area in {"time", "clock"}:
            if not tokens[1:]:
                return await utils.answer(message, self._clock_status())
            if tokens[1].lower() == "clear" and len(tokens) == 2:
                return await utils.answer(message, self._clear_user_clock())
            if tokens[1].lower() == "set":
                value = raw.split(None, 2)[2] if len(raw.split(None, 2)) == 3 else ""
                return await utils.answer(message, self._set_user_clock(value))
            return await utils.answer(message, self.strings("time_usage"))
        if area in {"limit", "limits"}:
            return await utils.answer(message, self.strings("reset_usage"))
        await utils.answer(message, self.strings("usage"))
