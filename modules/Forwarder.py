# ⟡ why-morphy — Shizu modules
"""Forward incoming messages from one private chat to a Telegram channel."""

import logging
import time

from pyrogram import Client, enums, errors, types

from .. import loader, utils


logger = logging.getLogger(__name__)
ERROR_NOTICE_COOLDOWN = 5 * 60


@loader.module("Forwarder", "morphy")
class ForwarderMod(loader.Module):
    """Forwards every new incoming message from a selected private chat."""

    strings = {
        "usage": (
            "<b>Forwarder</b>\n\n"
            "<code>.forwarder @channel</code> — use the current private chat as the source\n"
            "<code>.forwarder @source @channel</code> — set both chats from anywhere\n"
            "<code>.forwarder status</code> — show the active route\n"
            "<code>.forwarder off</code> — stop forwarding\n\n"
            "The account running Shizu must be able to read the source chat and post in the channel. "
            "Protected-content messages cannot be forwarded by Telegram."
        ),
        "configured": (
            "✅ Forwarding new messages from <b>{source}</b> to <b>{target}</b>.\n"
            "Use <code>.forwarder off</code> to stop it."
        ),
        "disabled": "✅ Anonymous forwarding is disabled.",
        "not_configured": "ℹ️ No forwarding route is configured.",
        "status": "📨 Forwarding <b>{source}</b> → <b>{target}</b>",
        "source_private": "🚫 The source must be a private chat or bot.",
        "current_private": "🚫 Run this inside the source private chat, or provide both <code>@source_bot @channel</code>.",
        "target_channel": "🚫 The destination must be a channel.",
        "same_chat": "🚫 Source and destination must be different chats.",
        "resolve_failed": "🚫 Couldn't open that chat: <code>{error}</code>",
        "forward_failed": (
            "⚠️ <b>Forwarder stopped one message</b>\n"
            "From: <code>{source}</code>\nTo: <code>{target}</code>\n"
            "Reason: <code>{error}</code>\n\n"
            "Check that you can still read the source, post in the channel, and that the message is not protected."
        ),
    }

    def __init__(self):
        self._last_error: tuple[str, str] | None = None
        self._last_error_at = 0.0

    async def on_load(self, app: Client):
        """Keep an existing route when upgrading from the old module name."""
        if self.db.get(self.name, "route", None) is None:
            route = self.db.get("AnonymousForwarder", "route", None)
            if isinstance(route, dict):
                self.db.set(self.name, "route", route)

    def _route(self) -> dict | None:
        route = self.db.get(self.name, "route", None)
        if not isinstance(route, dict):
            return None
        if not isinstance(route.get("source"), int) or not isinstance(route.get("target"), int):
            return None
        return route

    @staticmethod
    def _chat_label(chat: types.Chat) -> str:
        username = getattr(chat, "username", None)
        if username:
            return f"@{username}"
        return getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat.id)

    @staticmethod
    def _is_private(chat: types.Chat) -> bool:
        return chat.type in (enums.ChatType.PRIVATE, enums.ChatType.BOT)

    async def _notify_forward_error(
        self, app: Client, route: dict, exc: Exception
    ) -> None:
        error_key = (type(exc).__name__, str(exc))
        now = time.monotonic()
        if error_key == self._last_error and now - self._last_error_at < ERROR_NOTICE_COOLDOWN:
            return
        self._last_error, self._last_error_at = error_key, now
        logger.warning("Forwarder: %s", exc)
        try:
            await app.send_message(
                "me",
                self.strings("forward_failed").format(
                    source=route["source"],
                    target=route["target"],
                    error=str(exc)[:300],
                ),
            )
        except errors.RPCError:
            logger.exception("Forwarder: unable to report forwarding failure")

    @loader.command()
    @loader.owner
    async def forwarder(self, app: Client, message: types.Message):
        """[@channel | @source @channel | status | off] - Forward a private chat to a channel"""
        args = utils.get_args_raw(message).split()
        if not args or args[0].lower() in {"help", "?"}:
            return await utils.answer(message, self.strings("usage"))

        action = args[0].lower()
        if action in {"off", "disable", "stop"} and len(args) == 1:
            self.db.pop(self.name, "route", None)
            return await utils.answer(message, self.strings("disabled"))
        if action in {"status", "show"} and len(args) == 1:
            route = self._route()
            if route is None:
                return await utils.answer(message, self.strings("not_configured"))
            return await utils.answer(
                message,
                self.strings("status").format(
                    source=route["source"], target=route["target"]
                ),
            )

        if len(args) == 1:
            if not self._is_private(message.chat):
                return await utils.answer(message, self.strings("current_private"))
            source_ref, target_ref = message.chat.id, args[0]
        elif len(args) == 2:
            source_ref, target_ref = args
        else:
            return await utils.answer(message, self.strings("usage"))

        try:
            source = await app.get_chat(source_ref)
            target = await app.get_chat(target_ref)
        except errors.RPCError as exc:
            return await utils.answer(
                message, self.strings("resolve_failed").format(error=str(exc)[:300])
            )

        if not self._is_private(source):
            return await utils.answer(message, self.strings("source_private"))
        if target.type != enums.ChatType.CHANNEL:
            return await utils.answer(message, self.strings("target_channel"))
        if source.id == target.id:
            return await utils.answer(message, self.strings("same_chat"))

        self.db.set(self.name, "route", {"source": source.id, "target": target.id})
        self._last_error = None
        return await utils.answer(
            message,
            self.strings("configured").format(
                source=self._chat_label(source), target=self._chat_label(target)
            ),
        )

    @loader.watcher("in", "only_pm")
    async def forward_watcher(self, app: Client, message: types.Message):
        route = self._route()
        if route is None or message.chat.id != route["source"]:
            return
        if not message.id or getattr(message, "empty", False):
            return

        try:
            await app.forward_messages(
                chat_id=route["target"],
                from_chat_id=route["source"],
                message_ids=message.id,
            )
        except errors.RPCError as exc:
            await self._notify_forward_error(app, route, exc)
