#    Sh1t-UB (telegram userbot by sh1tn3t)
#    Copyright (C) 2021-2022 Sh1tN3t
#    Integrated into Shizu by why-morphy

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.

#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import random

from pyrogram import Client, enums, errors, types

from .. import loader, utils

logger = logging.getLogger(__name__)

DEFAULT_CHANCE = 30
# Used in chats that allow every reaction, since Telegram doesn't list them there
DEFAULT_REACTIONS = ["👍", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "🎉", "🤩", "👌", "😍"]


@loader.module("RandomReact", "sh1tn3t")
class RandomReactMod(loader.Module):
    """Puts random reactions on messages in chosen groups"""

    strings = {
        "cfg_reactions": "Reactions to use in chats that allow any reaction",
        "on": "👍 Random reactions <b>on</b>\nChance: <b>{}%</b>",
        "off": "👎 Random reactions <b>off</b>",
        "not_group": "🚫 Use this in a group",
        "no_reactions": "🚫 Reactions are disabled in this chat",
    }

    strings_ru = {
        "cfg_reactions": "Реакции для чатов, где разрешены любые реакции",
        "on": "👍 Рандомные реакции <b>включены</b>\nШанс: <b>{}%</b>",
        "off": "👎 Рандомные реакции <b>выключены</b>",
        "not_group": "🚫 Используй в группе",
        "no_reactions": "🚫 В этом чате реакции отключены",
    }

    def __init__(self):
        # Newest message id seen per chat. Shizu also feeds edits to watchers, and
        # Telegram reports reaction changes as edits, so without this every reaction
        # on a message (ours included) would roll the chance again
        self._last_seen: dict[int, int] = {}
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "reactions",
                DEFAULT_REACTIONS,
                lambda: self.strings("cfg_reactions"),
                validator=loader.validators.Series(),
            ),
        )

    async def _chat_reactions(self, app: Client, chat_id: int) -> list[str] | None:
        """Emoji the chat allows; None means any, [] means reactions are off"""
        available = (await app.get_chat(chat_id)).available_reactions
        if not available:
            return []
        if available.all_are_enabled:
            return None
        return [r.emoji for r in available.reactions or [] if r.emoji]

    @loader.command()
    async def react(self, app: Client, message: types.Message):
        """[chance 1-100] - Turn random reactions on (or change the chance) or off in this group"""
        if message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            return await utils.answer(message, self.strings("not_group"))

        args = utils.get_args_raw(message)
        chat_id = str(message.chat.id)
        chats = self.db.get("RandomReact", "chats", {})

        if not args and chat_id in chats:
            del chats[chat_id]
            self.db.set("RandomReact", "chats", chats)
            return await utils.answer(message, self.strings("off"))

        reactions = await self._chat_reactions(app, message.chat.id)
        if reactions == []:
            return await utils.answer(message, self.strings("no_reactions"))

        chance = int(args) if args.isdigit() and 0 < int(args) <= 100 else DEFAULT_CHANCE
        chats[chat_id] = {"chance": chance, "reactions": reactions}
        self.db.set("RandomReact", "chats", chats)
        await utils.answer(message, self.strings("on").format(chance))

    @loader.watcher("in", "only_groups")
    async def watcher(self, app: Client, message: types.Message):
        chat = self.db.get("RandomReact", "chats", {}).get(str(message.chat.id))
        if not chat or message.edit_date:
            return
        if message.id <= self._last_seen.get(message.chat.id, 0):
            return
        self._last_seen[message.chat.id] = message.id

        if random.randint(1, 100) > chat["chance"]:
            return

        reactions = chat["reactions"] or self.config["reactions"]
        if not reactions:
            return

        try:
            await app.send_reaction(
                message.chat.id,
                message_id=message.id,
                emoji=random.choice(reactions),
            )
        except errors.RPCError as e:
            # The chat may have changed its reactions since; run .react again to refresh
            logger.debug("RandomReact: %s", e)
