# required: aiohttp

# ⟡ why-morphy — Shizu modules

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
from pyrogram import Client, types
from pyrogram.types import CallbackQuery

from .. import loader, utils

try:
    from ..telegram.rich import button_rows
except ImportError:
    button_rows = None

SOURCES = {
    # Coinbase: no key, fiat + crypto (~650 currencies), refreshed every minute
    "coinbase": "https://api.coinbase.com/v2/exchange-rates?currency={}",
    # ExchangeRate-API open access: no key, ~165 fiat currencies, refreshed daily
    "erapi": "https://open.er-api.com/v6/latest/{}",
    # Central Bank of Uzbekistan: official rates of ~75 currencies against UZS,
    # set once per business day. Other pairs are cross rates through UZS.
    "cbu": "https://cbu.uz/ru/arkhiv-kursov-valyut/json/",
}
DIRECTIONS = ("both", "up", "down")
HISTORY_DAYS = 30
HISTORY_MAX = 3000
SPARK = "▁▂▃▄▅▆▇█"
DAY = 86400


def fmt_rate(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.6g}"


def fmt_pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def pct(new: float, old: float) -> float:
    return (new - old) / old * 100


@loader.module("CurrencyTracker", "morphy")
class CurrencyTrackerMod(loader.Module):
    """Tracks exchange rates and alerts when they rise or fall past a threshold"""

    strings = {
        "usage": (
            "🚫 Usage: <code>.track USD UZS [percent] [up|down|both] [coinbase|erapi|cbu]</code>\n"
            "Example: <code>.track USD UZS 0.5 down</code>"
        ),
        "usage_pair": "🚫 Pass a pair, e.g. <code>USD UZS</code> or <code>BTC/USD</code>",
        "unknown": "🚫 No rate for <code>{}</code> from {}",
        "fetch_failed": "🚫 Couldn't fetch rates: <code>{}</code>",
        "tracked": (
            "👀 Tracking <b>{pair}</b> at <b>{rate}</b> ({source})\n"
            "Alert when it moves {dir} by <b>{threshold}%</b>"
        ),
        "untracked": "🗑 Stopped tracking <b>{}</b>",
        "not_tracked": "🚫 <b>{}</b> isn't tracked",
        "empty": "📭 Nothing tracked yet. Add a pair: <code>.track USD UZS</code>",
        "dir_both": "up or down",
        "dir_up": "up",
        "dir_down": "down",
        "rates_head": "💱 <b>Tracked rates</b>",
        "rates_foot": "🕒 {} UTC",
        "row": (
            "{icon} <b>{pair}</b> {rate}\n"
            "    24h {d1} · 7d {d7} · since alert {dref}\n"
            "    alert {dir} {threshold}% · {source} {spark}"
        ),
        "single": "💱 <b>{pair}</b> = <b>{rate}</b> · {source}\n{amount_line}",
        "trend": "24h {d1} · 7d {d7} · 30d {d30}\nRange 7d: {low} – {high}\n{spark}",
        "amount_line": "{amount} {base} = <b>{total}</b> {quote}\n",
        "alert_up": "📈 <b>{pair}</b> rose <b>{change}</b>",
        "alert_down": "📉 <b>{pair}</b> fell <b>{change}</b>",
        "alert_body": "\n{old} → <b>{new}</b> · {source}\n24h {d1} · 7d {d7}",
        "btn_refresh": "🔄 Refresh",
        "cfg_interval": "Minutes between checks",
        "cfg_threshold": "Default alert threshold in percent",
        "cfg_chat": "Where alerts go: me, a chat id or @username",
        "cfg_source": (
            "Default source for new pairs: cbu (Central Bank of Uzbekistan, official, daily), "
            "coinbase (fiat + crypto, live) or erapi (fiat, daily)"
        ),
    }

    strings_ru = {
        "usage": (
            "🚫 Использование: <code>.track USD UZS [процент] [up|down|both] [coinbase|erapi|cbu]</code>\n"
            "Пример: <code>.track USD UZS 0.5 down</code>"
        ),
        "usage_pair": "🚫 Укажи пару, например <code>USD UZS</code> или <code>BTC/USD</code>",
        "unknown": "🚫 Нет курса <code>{}</code> в {}",
        "fetch_failed": "🚫 Не удалось получить курсы: <code>{}</code>",
        "tracked": (
            "👀 Слежу за <b>{pair}</b>, сейчас <b>{rate}</b> ({source})\n"
            "Сообщу, когда курс изменится {dir} на <b>{threshold}%</b>"
        ),
        "untracked": "🗑 Больше не слежу за <b>{}</b>",
        "not_tracked": "🚫 <b>{}</b> не отслеживается",
        "empty": "📭 Пока ничего не отслеживается. Добавь пару: <code>.track USD UZS</code>",
        "dir_both": "вверх или вниз",
        "dir_up": "вверх",
        "dir_down": "вниз",
        "rates_head": "💱 <b>Отслеживаемые курсы</b>",
        "rates_foot": "🕒 {} UTC",
        "row": (
            "{icon} <b>{pair}</b> {rate}\n"
            "    24ч {d1} · 7д {d7} · с алерта {dref}\n"
            "    алерт {dir} {threshold}% · {source} {spark}"
        ),
        "single": "💱 <b>{pair}</b> = <b>{rate}</b> · {source}\n{amount_line}",
        "trend": "24ч {d1} · 7д {d7} · 30д {d30}\nДиапазон 7д: {low} – {high}\n{spark}",
        "amount_line": "{amount} {base} = <b>{total}</b> {quote}\n",
        "alert_up": "📈 <b>{pair}</b> вырос на <b>{change}</b>",
        "alert_down": "📉 <b>{pair}</b> упал на <b>{change}</b>",
        "alert_body": "\n{old} → <b>{new}</b> · {source}\n24ч {d1} · 7д {d7}",
        "btn_refresh": "🔄 Обновить",
        "cfg_interval": "Минут между проверками",
        "cfg_threshold": "Порог алерта по умолчанию, в процентах",
        "cfg_chat": "Куда слать алерты: me, id чата или @username",
        "cfg_source": (
            "Источник по умолчанию для новых пар: cbu (ЦБ Узбекистана, официальный, раз в день), "
            "coinbase (валюты + крипта, онлайн) или erapi (валюты, раз в день)"
        ),
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            "interval", 15, lambda: self.strings("cfg_interval"),
            "threshold", 1.0, lambda: self.strings("cfg_threshold"),
            "alert_chat", "me", lambda: self.strings("cfg_chat"),
            "source", "cbu", lambda: self.strings("cfg_source"),
        )  # fmt: skip

    async def on_load(self, app: Client):
        self._lock = asyncio.Lock()
        self._cache = {}

    # ----------------------------------------------------------------- rates

    @property
    def _source(self) -> str:
        source = str(self.config["source"]).lower()
        return source if source in SOURCES else "cbu"

    def _pair_source(self, info: dict) -> str:
        # Pairs tracked before sources were pinned came from Coinbase, the old default.
        return info.get("source") or "coinbase"

    async def _fetch_base(self, source: str, base: str) -> dict:
        """All rates for `base` from `source`, as {QUOTE: quote units per 1 base}"""
        cached = self._cache.get((source, base))
        if cached and time.time() - cached[0] < 30:
            return cached[1]

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            async with session.get(SOURCES[source].format(base)) as resp:
                data = await resp.json(content_type=None)

        if source == "coinbase":
            raw = data["data"]["rates"]
        elif source == "erapi":
            if data.get("result") != "success":
                raise ValueError(data.get("error-type", "error"))
            raw = data["rates"]
        else:
            raw = self._cbu_rates(data, base)
        rates = {k.upper(): float(v) for k, v in raw.items()}
        self._cache[(source, base)] = (time.time(), rates)
        return rates

    @staticmethod
    def _cbu_rates(data: list, base: str) -> dict:
        # CBU gives the UZS price of `Nominal` units of each currency.
        uzs = {"UZS": 1.0}
        for item in data:
            uzs[item["Ccy"].upper()] = float(item["Rate"]) / float(item["Nominal"] or 1)
        if not uzs.get(base):
            return {}
        return {quote: uzs[base] / value for quote, value in uzs.items() if value}

    async def _fetch_pairs(self, pairs: dict) -> dict:
        """{pair: rate} for {"BASE/QUOTE": source}; pairs that can't be fetched are left out"""
        keys = list({(source, pair.split("/")[0]) for pair, source in pairs.items()})
        results = await asyncio.gather(
            *(self._fetch_base(*key) for key in keys), return_exceptions=True
        )
        fetched = {k: r for k, r in zip(keys, results) if not isinstance(r, Exception)}
        if not fetched and results:
            raise results[0]
        rates = {}
        for pair, source in pairs.items():
            base, quote = pair.split("/")
            if (rate := fetched.get((source, base), {}).get(quote)) is not None:
                rates[pair] = rate
        return rates

    async def _find_rate(self, pair: str, source: str = None) -> tuple:
        """(source, rate) from `source`, or from the first source that has the pair.
        Raises RuntimeError with a ready-to-send message."""
        if source:
            order = [source]
        else:
            order = [self._source, *(s for s in SOURCES if s != self._source)]
        errors = []
        for name in order:
            try:
                rate = (await self._fetch_pairs({pair: name})).get(pair)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            if rate is not None:
                return name, rate
        if len(errors) == len(order):
            raise RuntimeError(
                self.strings("fetch_failed").format(utils.escape_html("; ".join(errors)))
            )
        raise RuntimeError(self.strings("unknown").format(pair, ", ".join(order)))

    # --------------------------------------------------------------- history

    def _history(self, pair: str) -> list:
        return self.get(f"history:{pair}", [])

    def _record(self, pair: str, rate: float, now: float):
        history = self._history(pair)
        if history and now - history[-1][0] < 60:
            history[-1] = [now, rate]
        else:
            history.append([now, rate])
        history = [p for p in history if now - p[0] <= HISTORY_DAYS * DAY]
        self.set(f"history:{pair}", history[-HISTORY_MAX:])

    @staticmethod
    def _change_since(history: list, rate: float, seconds: int):
        """Percent change against the oldest point within `seconds`, if history reaches back that far"""
        if not history:
            return None
        target = time.time() - seconds
        if history[0][0] > target + seconds * 0.1:
            return None  # history too short to say
        past = next((r for t, r in history if t >= target), history[-1][1])
        return pct(rate, past)

    @staticmethod
    def _spark(history: list, seconds: int = 7 * DAY, width: int = 16) -> str:
        cutoff = time.time() - seconds
        values = [r for t, r in history if t >= cutoff]
        if len(values) < 2:
            return ""
        step = max(1, len(values) // width)
        values = values[::step][-width:]
        low, high = min(values), max(values)
        if high == low:
            return SPARK[3] * len(values)
        return "".join(
            SPARK[round((v - low) / (high - low) * (len(SPARK) - 1))] for v in values
        )

    # ------------------------------------------------------------------ args

    @staticmethod
    def _parse_pair(tokens: list):
        """Takes the pair off the front of `tokens`: "USD UZS" or "USD/UZS" """
        if tokens and "/" in tokens[0]:
            base, _, quote = tokens.pop(0).partition("/")
        elif len(tokens) >= 2:
            base, quote = tokens.pop(0), tokens.pop(0)
        else:
            return None
        base, quote = base.upper(), quote.upper()
        if not (base.isalnum() and quote.isalnum()) or base == quote:
            return None
        return f"{base}/{quote}"

    def _dir_text(self, direction: str) -> str:
        return self.strings(f"dir_{direction}")

    # ---------------------------------------------------------------- alerts

    @loader.loop(interval=60, autostart=True)
    async def check_loop(self):
        # The loop wakes every minute; the configured interval decides when to check.
        now = time.time()
        if now - self.get("last_check", 0) < max(1, int(self.config["interval"])) * 60:
            return
        self.set("last_check", now)

        async with self._lock:
            pairs = self.get("pairs", {})
            if not pairs:
                return
            # Each pair always uses its own source: sources differ by up to ~1%,
            # so mixing them would look like a real move and fire false alerts.
            rates = await self._fetch_pairs(
                {pair: self._pair_source(info) for pair, info in pairs.items()}
            )
            alerts = []
            for pair, info in pairs.items():
                if (rate := rates.get(pair)) is None:
                    continue
                self._record(pair, rate, now)
                change = pct(rate, info["ref"])
                if abs(change) < info["threshold"]:
                    continue
                direction = "up" if change > 0 else "down"
                if info["direction"] in ("both", direction):
                    alerts.append(
                        self._alert_text(pair, info, rate, change)
                    )
                # Move the reference either way, so the next alert measures from here.
                info["ref"], info["ref_ts"] = rate, now
            self.set("pairs", pairs)

        chat = str(self.config["alert_chat"]).strip() or "me"
        if chat.lstrip("-").isdigit():
            chat = int(chat)
        for text in alerts:
            await self.app.send_message(chat, text)

    def _alert_text(self, pair: str, info: dict, new: float, change: float) -> str:
        history = self._history(pair)
        head = self.strings("alert_up" if change > 0 else "alert_down")
        return head.format(pair=pair, change=f"{abs(change):.2f}%") + self.strings(
            "alert_body"
        ).format(
            old=fmt_rate(info["ref"]),
            new=fmt_rate(new),
            source=self._pair_source(info),
            d1=fmt_pct(self._change_since(history, new, DAY)),
            d7=fmt_pct(self._change_since(history, new, 7 * DAY)),
        )

    # -------------------------------------------------------------- commands

    @loader.command()
    async def track(self, app: Client, message: types.Message):
        """Track a pair: .track USD UZS [percent] [up|down|both] [coinbase|erapi|cbu]"""
        tokens = utils.get_args_raw(message).split()
        if not (pair := self._parse_pair(tokens)):
            return await utils.answer(message, self.strings("usage"))

        threshold, direction, source = float(self.config["threshold"]), "both", None
        for token in tokens:
            if token.lower() in DIRECTIONS:
                direction = token.lower()
                continue
            if token.lower() in SOURCES:
                source = token.lower()
                continue
            try:
                threshold = abs(float(token.rstrip("%").replace(",", ".")))
            except ValueError:
                return await utils.answer(message, self.strings("usage"))
        if not threshold:
            return await utils.answer(message, self.strings("usage"))

        try:
            source, rate = await self._find_rate(pair, source)
        except RuntimeError as e:
            return await utils.answer(message, str(e))

        now = time.time()
        async with self._lock:
            pairs = self.get("pairs", {})
            if pair in pairs and self._pair_source(pairs[pair]) != source:
                self.set(f"history:{pair}", [])  # don't mix two sources in one chart
            pairs[pair] = {
                "threshold": threshold,
                "direction": direction,
                "source": source,
                "ref": rate,
                "ref_ts": now,
            }
            self.set("pairs", pairs)
            self._record(pair, rate, now)

        await utils.answer(
            message,
            self.strings("tracked").format(
                pair=pair,
                rate=fmt_rate(rate),
                source=source,
                dir=self._dir_text(direction),
                threshold=f"{threshold:g}",
            ),
        )

    @loader.command()
    async def untrack(self, app: Client, message: types.Message):
        """Stop tracking a pair: .untrack USD UZS"""
        if not (pair := self._parse_pair(utils.get_args_raw(message).split())):
            return await utils.answer(message, self.strings("usage_pair"))
        async with self._lock:
            pairs = self.get("pairs", {})
            if pairs.pop(pair, None) is None:
                return await utils.answer(
                    message, self.strings("not_tracked").format(pair)
                )
            self.set("pairs", pairs)
            self.set(f"history:{pair}", [])
        await utils.answer(message, self.strings("untracked").format(pair))

    async def _rates_text(self) -> str:
        pairs = self.get("pairs", {})
        if not pairs:
            return self.strings("empty")

        rates = await self._fetch_pairs(
            {pair: self._pair_source(info) for pair, info in pairs.items()}
        )
        now = time.time()
        rows = []
        for pair, info in pairs.items():
            if (rate := rates.get(pair)) is None:
                continue
            self._record(pair, rate, now)
            history = self._history(pair)
            d1 = self._change_since(history, rate, DAY)
            trend = d1 if d1 is not None else pct(rate, info["ref"])
            rows.append(
                self.strings("row").format(
                    icon="📈" if trend > 0 else "📉" if trend < 0 else "➖",
                    pair=pair,
                    rate=fmt_rate(rate),
                    d1=fmt_pct(d1),
                    d7=fmt_pct(self._change_since(history, rate, 7 * DAY)),
                    dref=fmt_pct(pct(rate, info["ref"])),
                    dir=self._dir_text(info["direction"]),
                    threshold=f"{info['threshold']:g}",
                    source=self._pair_source(info),
                    spark=self._spark(history),
                )
            )
        stamp = datetime.now(timezone.utc).strftime("%H:%M")
        return "\n\n".join(
            [
                self.strings("rates_head"),
                *rows,
                self.strings("rates_foot").format(stamp),
            ]
        )

    def _rates_markup(self):
        return [[{"text": self.strings("btn_refresh"), "callback": self.inline__refresh}]]

    @staticmethod
    def _rich_rates(text: str, markup: list | None) -> dict | None:
        """Render the rate dashboard as rich cards, with a text fallback."""
        if button_rows is None or not markup:
            return None
        sections = [section for section in text.split("\n\n") if section.strip()]
        if len(sections) < 2:
            return None
        title, *cards = sections
        footer = cards.pop() if cards else ""
        html = f"<h2>{title}</h2>" + "".join(
            f"<blockquote>{card.replace(chr(10), '<br>')}</blockquote>"
            for card in cards
        )
        if footer:
            html += f"<blockquote>{footer}</blockquote>"
        return {"html": html + button_rows(markup)}

    @loader.command()
    async def rates(self, app: Client, message: types.Message):
        """Show tracked pairs with 24h/7d changes"""
        try:
            text = await self._rates_text()
        except Exception as e:
            return await utils.answer(
                message, self.strings("fetch_failed").format(utils.escape_html(str(e)))
            )
        markup = self._rates_markup() if self.get("pairs", {}) else None
        rich = self._rich_rates(text, markup)
        await utils.answer(message, text, reply_markup=markup, rich_message=rich)

    async def inline__refresh(self, call: CallbackQuery):
        self._cache.clear()
        try:
            text = await self._rates_text()
        except Exception as e:
            return await call.answer(str(e)[:200])
        markup = self._rates_markup()
        rich = self._rich_rates(text, markup)
        await call.edit(text, reply_markup=markup, rich_message=rich)

    @loader.command()
    async def rate(self, app: Client, message: types.Message):
        """Current rate and trend: .rate USD UZS [amount] [coinbase|erapi|cbu]"""
        tokens = utils.get_args_raw(message).split()
        if not (pair := self._parse_pair(tokens)):
            return await utils.answer(message, self.strings("usage_pair"))

        source = next((t.lower() for t in tokens if t.lower() in SOURCES), None)
        tokens = [t for t in tokens if t.lower() not in SOURCES]
        tracked = self.get("pairs", {}).get(pair)
        if tracked and not source:
            source = self._pair_source(tracked)
        try:
            source, rate = await self._find_rate(pair, source)
        except RuntimeError as e:
            return await utils.answer(message, str(e))
        # Trends only exist for tracked pairs, and only in their own source.
        same_source = bool(tracked) and self._pair_source(tracked) == source

        base, quote = pair.split("/")
        amount_line = ""
        if tokens:
            try:
                amount = float(tokens[0].replace(",", "."))
                amount_line = self.strings("amount_line").format(
                    amount=f"{amount:g}", base=base, total=fmt_rate(amount * rate), quote=quote
                )
            except ValueError:
                pass

        if same_source:
            self._record(pair, rate, time.time())
        text = self.strings("single").format(
            pair=pair, rate=fmt_rate(rate), source=source, amount_line=amount_line
        )
        history = self._history(pair) if same_source else []
        if len(history) > 1:
            week = [r for t, r in history if t >= time.time() - 7 * DAY] or [rate]
            text += self.strings("trend").format(
                d1=fmt_pct(self._change_since(history, rate, DAY)),
                d7=fmt_pct(self._change_since(history, rate, 7 * DAY)),
                d30=fmt_pct(self._change_since(history, rate, 30 * DAY)),
                low=fmt_rate(min(week)),
                high=fmt_rate(max(week)),
                spark=self._spark(history),
            )
        await utils.answer(message, text)
