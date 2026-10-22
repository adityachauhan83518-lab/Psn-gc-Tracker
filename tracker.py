import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import aiohttp
from bs4 import BeautifulSoup
import discord
from discord import app_commands
from discord.ext import tasks


logger = logging.getLogger("psn_stock_tracker")


class StockState(str, Enum):
    IN_STOCK = "In stock"
    OUT_OF_STOCK = "Out of stock"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class Store:
    name: str
    url: str


@dataclass(frozen=True)
class StoreResult:
    store: Store
    state: StockState
    detail: str


STORES = (
    Store("JioGames", "https://store.jiogames.com/products/?brand=playstation"),
    Store("Rooter Shop", "https://shop.rooter.gg/product/playstation-gift-cards"),
    Store("Woohoo", "https://www.woohoo.in/playstation-e-gift-instant-voucher"),
    Store("Gyftr", "https://www.gyftr.com/sony-playstation"),
    Store(
        "Games The Shop",
        "https://www.gamestheshop.com/product/2153a461-7203-4802-aac4-283be4c775d3?variant=09df672e-b598-4b2d-9b3e-e1ca1dd9d205",
    ),
    Store("Hubble", "https://offers.myhubble.money/shop-by-category/new-brands-gift-cards"),
    Store("Stan Shop", "https://www.stanshop.co/playstation-psn-gift-card-in-india"),
    Store(
        "HGWorld",
        "https://hgworld.in/shop/rs-1000-playstation-network-wallet-top-up-psn-card-india-only-same-day-delivery-on-whatsapp-copy/",
    ),
)

TARGET_PATTERN = re.compile(
    r"(?:₹|\brs\.?\s*|\binr\s*)\s*1[\s,]?000\b|\b1[\s,]?000\s*(?:₹|\brs\.?\b|\binr\b)",
    re.IGNORECASE,
)
OUT_OF_STOCK = (
    "out of stock",
    "sold out",
    "currently unavailable",
    "not available",
    "notify me when available",
)
IN_STOCK = (
    "in stock",
    "available now",
    "add to cart",
    "add to bag",
    "buy now",
    "buy it now",
)


def classify_page(html: str) -> tuple[StockState, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings)
    normalized = re.sub(r"\s+", " ", text).casefold()

    if not ("playstation" in normalized or "psn" in normalized) or not TARGET_PATTERN.search(normalized):
        return StockState.UNKNOWN, "The exact ₹1,000 PlayStation product was not found."

    matching_nodes = []
    for node in soup.find_all(True):
        node_text = re.sub(r"\s+", " ", " ".join(node.stripped_strings)).casefold()
        if (
            ("playstation" in node_text or "psn" in node_text)
            and TARGET_PATTERN.search(node_text)
        ):
            matching_nodes.append((len(node_text), node))

    if not matching_nodes:
        return StockState.UNKNOWN, "The product was found, but its stock status could not be isolated."

    _, product_node = min(matching_nodes, key=lambda item: item[0])
    product_text = ""
    current = product_node
    for _ in range(6):
        product_text = re.sub(r"\s+", " ", " ".join(current.stripped_strings)).casefold()
        if any(marker in product_text for marker in OUT_OF_STOCK + IN_STOCK):
            break
        if current.parent is None or current.parent.name in ("body", "html"):
            break
        current = current.parent

    has_out_signal = any(marker in product_text for marker in OUT_OF_STOCK)
    has_in_signal = any(marker in product_text for marker in IN_STOCK)
    if has_out_signal and not has_in_signal:
        return StockState.OUT_OF_STOCK, "The product section indicates it is unavailable."
    if has_in_signal and not has_out_signal:
        return StockState.IN_STOCK, "The product section indicates it can be purchased."
    return StockState.UNKNOWN, "The product was found, but stock evidence is missing or conflicting."


async def check_store(session: aiohttp.ClientSession, store: Store) -> StoreResult:
    try:
        async with session.get(store.url) as response:
            if response.status >= 400:
                return StoreResult(store, StockState.UNKNOWN, f"HTTP {response.status}")
            html = await response.text(errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        return StoreResult(store, StockState.UNKNOWN, f"Request failed: {type(error).__name__}")

    state, detail = classify_page(html)
    return StoreResult(store, state, detail)


async def check_all_stores() -> list[StoreResult]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": "PSNStockTracker/1.0 (personal stock monitoring bot)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        return await asyncio.gather(*(check_store(session, store) for store in STORES))


def should_alert(previous: StockState | None, current: StockState) -> bool:
    return current is StockState.IN_STOCK and previous is not StockState.IN_STOCK


def format_report(results: list[StoreResult]) -> str:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"PSN ₹1,000 stock status | {checked_at}"]
    lines.extend(
        f"- [{result.store.name}]({result.store.url}): {result.state.value} ({result.detail})"
        for result in results
    )
    return "\n".join(lines)


def format_stock_alert(store: Store) -> str:
    return (
        f"Stock detected: {store.name} may have the PSN ₹1,000 wallet top-up in stock.\n"
        f"[Open {store.name}]({store.url})"
    )


def format_ping(last_check_at: datetime | None, polling: bool) -> str:
    if not polling:
        return "I'm alive, but store polling is not running."
    if last_check_at is None:
        return "I'm alive and polling every 30 seconds. The first store scan hasn't completed yet."

    checked_at = last_check_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"I'm alive and polling every 30 seconds. Last store scan completed at {checked_at}."


class TrackerBot(discord.Client):
    def __init__(self, channel_id: int, admin_user_id: int) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id = channel_id
        self.admin_user_id = admin_user_id
        self.last_states: dict[str, StockState] = {}
        self.last_check_at: datetime | None = None
        self.commands_synced = False
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="Check the latest stock status for all tracked stores.",
                callback=self.status_command,
            )
        )

    async def on_ready(self) -> None:
        if not self.commands_synced:
            try:
                await self.tree.sync()
                self.commands_synced = True
            except discord.DiscordException:
                logger.exception("Could not register Discord slash commands")
        if not self.poll_stores.is_running():
            self.poll_stores.start()
        logger.info("Connected to Discord as %s", self.user)

    async def status_command(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_user_id:
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await check_all_stores()
        await interaction.followup.send(format_report(results), ephemeral=True)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.content.strip().casefold() != "!ping":
            return
        await message.channel.send(
            format_ping(self.last_check_at, self.poll_stores.is_running())
        )

    @tasks.loop(seconds=30)
    async def poll_stores(self) -> None:
        try:
            results = await check_all_stores()
            self.last_check_at = datetime.now(timezone.utc)
            channel = self.get_channel(self.channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.channel_id)

            for result in results:
                store_name = result.store.name
                previous = self.last_states.get(store_name)
                if result.state is not StockState.UNKNOWN:
                    self.last_states[store_name] = result.state
                logger.info("%s: %s (%s)", store_name, result.state.value, result.detail)
                if should_alert(previous, result.state):
                    await channel.send(format_stock_alert(result.store))
        except (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("The scheduled stock check could not be completed")


def _required_env_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Set the {name} environment variable before starting the bot.")
    try:
        return int(value)
    except ValueError as error:
        raise SystemExit(f"{name} must be a numeric ID.") from error


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before starting the bot.")

    bot = TrackerBot(
        channel_id=_required_env_int("DISCORD_CHANNEL_ID"),
        admin_user_id=_required_env_int("DISCORD_ADMIN_USER_ID"),
    )
    bot.run(token)


if __name__ == "__main__":
    main()