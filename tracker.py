import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from urllib.parse import urljoin, urlsplit, urlunsplit

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


class DetectionMethod(str, Enum):
    API = "Public API"
    JSON_LD = "JSON-LD"
    EMBEDDED_JSON = "Embedded JSON"
    BROWSER = "Browser"
    HTML = "HTML"


@dataclass(frozen=True)
class Store:
    name: str
    url: str
    api_url: str | None = None


@dataclass(frozen=True)
class ProductObservation:
    name: str
    state: StockState
    price: str | None
    method: DetectionMethod
    conflicting: bool = False


@dataclass(frozen=True)
class StoreResult:
    store: Store
    state: StockState
    detail: str
    product_name: str | None = None
    price: str | None = None
    method: DetectionMethod | None = None


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
    r"(?:₹|\brs\.?\s*|\binr\s*)\s*1[\s,]?000\b"
    r"|\b1[\s,]?000\s*(?:₹|\brs\.?\b|\binr\b)"
    r"|\b1[\s,]?000\s*(?:wallet|top[ -]?up|gift[ -]?card|psn|playstation)\b"
    r"|\b(?:wallet|top[ -]?up|gift[ -]?card|psn|playstation)[\w\s-]{0,24}"
    r"1[\s,]?000\b",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(r"(?:₹|\bINR\s*|\bRs\.?\s*)([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
TARGET_PRICE_PATTERN = re.compile(
    r"(?:₹|\bINR\s*|\bRs\.?\s*)1[\s,]?000\b|\b1[\s,]?000\s*(?:₹|\bINR\b|\bRs\.?\b)",
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


def _is_target_product(name: str) -> bool:
    normalized = re.sub(r"\s+", " ", name).casefold()
    return ("playstation" in normalized or "psn" in normalized) and bool(
        TARGET_PATTERN.search(normalized)
    )


def _merge_states(states: list[StockState]) -> StockState:
    definitive = {state for state in states if state is not StockState.UNKNOWN}
    if len(definitive) > 1:
        return StockState.UNKNOWN
    return next(iter(definitive), StockState.UNKNOWN)


def _state_from_text(text: str) -> StockState:
    normalized = re.sub(r"\s+", " ", text).casefold()
    has_out = any(marker in normalized for marker in OUT_OF_STOCK) or any(
        marker in normalized for marker in ("outofstock", "soldout", "unavailable")
    )
    positive_text = re.sub(
        r"\b(?:currently\s+|temporarily\s+)?(?:not\s+available|unavailable)\b",
        " ",
        normalized,
    )
    has_in = any(marker in positive_text for marker in IN_STOCK) or any(
        marker in positive_text for marker in ("instock",)
    ) or bool(re.search(r"\bavailable\b", positive_text))
    if has_out and not has_in:
        return StockState.OUT_OF_STOCK
    if has_in and not has_out:
        return StockState.IN_STOCK
    return StockState.UNKNOWN


def _state_from_mapping(data: dict) -> tuple[StockState, bool]:
    states = []
    for key, value in data.items():
        normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
        if normalized_key in {"availability", "stockstatus", "inventoryavailability"}:
            if isinstance(value, str):
                states.append(_state_from_text(value))
        elif normalized_key in {"available", "isavailable", "instock", "isinstock", "purchasable"}:
            if isinstance(value, bool):
                states.append(StockState.IN_STOCK if value else StockState.OUT_OF_STOCK)
        elif normalized_key in {"inventoryquantity", "stockquantity", "quantityinstock", "qty"}:
            try:
                quantity = float(value)
            except (TypeError, ValueError):
                continue
            states.append(StockState.IN_STOCK if quantity > 0 else StockState.OUT_OF_STOCK)
        elif normalized_key in {"status", "stock"} and isinstance(value, str):
            parsed = _state_from_text(value)
            if parsed is not StockState.UNKNOWN:
                states.append(parsed)
    definitive = {state for state in states if state is not StockState.UNKNOWN}
    return _merge_states(states), len(definitive) > 1


def _format_price(value, currency: str = "INR", minor_unit: int | None = None) -> str | None:
    if isinstance(value, dict):
        currency = str(value.get("currency", value.get("currencyCode", currency)))
        minor_unit = value.get("currency_minor_unit", minor_unit)
        value = value.get("amount", value.get("value", value.get("price")))
    if value is None or isinstance(value, bool):
        return None
    raw = re.sub(r"[^\d.,-]", "", str(value)).replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    if minor_unit is not None:
        try:
            amount /= Decimal(10) ** int(minor_unit)
        except (TypeError, ValueError, InvalidOperation):
            return None
    elif isinstance(value, int) and value >= 10000:
        amount /= Decimal(100)
    formatted = f"{amount:,.2f}".rstrip("0").rstrip(".")
    if currency.upper() in {"INR", "RS", "₹"}:
        return f"₹{formatted}"
    return f"{currency.upper()} {formatted}"


def _price_from_mapping(data: dict) -> str | None:
    currency = str(data.get("priceCurrency", data.get("currency_code", data.get("currency", "INR"))))
    minor_unit = data.get("currency_minor_unit")
    prices = data.get("prices")
    if isinstance(prices, dict):
        return _format_price(prices, currency, minor_unit)
    for key in (
        "salePrice",
        "sale_price",
        "sellingPrice",
        "selling_price",
        "offerPrice",
        "offer_price",
        "price",
        "amount",
        "regularPrice",
        "regular_price",
    ):
        if key in data:
            return _format_price(data[key], currency, minor_unit)
    return None


def _product_observations_from_json(data, method: DetectionMethod) -> list[ProductObservation]:
    observations = []

    def visit(value, inherited_name: str | None = None) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, inherited_name)
            return
        if not isinstance(value, dict):
            return

        own_name = next(
            (
                str(value[key]).strip()
                for key in ("name", "title", "productName", "product_name", "productTitle", "product_title")
                if isinstance(value.get(key), (str, int, float)) and str(value[key]).strip()
            ),
            None,
        )
        if own_name and inherited_name and own_name.casefold() not in inherited_name.casefold():
            product_name = own_name if _is_target_product(own_name) else f"{inherited_name} {own_name}"
        else:
            product_name = own_name or inherited_name

        if product_name and _is_target_product(product_name):
            state, conflicting = _state_from_mapping(value)
            observations.append(
                ProductObservation(
                    name=product_name,
                    state=state,
                    price=_price_from_mapping(value),
                    method=method,
                    conflicting=conflicting,
                )
            )

        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child, product_name)

    visit(data)
    return observations


def _choose_observation(observations: list[ProductObservation]) -> ProductObservation | None:
    if not observations:
        return None
    states = [observation.state for observation in observations]
    state = _merge_states(states)
    conflicting = any(observation.conflicting for observation in observations) or len(
        {candidate for candidate in states if candidate is not StockState.UNKNOWN}
    ) > 1
    best = min(
        observations,
        key=lambda observation: (not bool(observation.price), len(observation.name)),
    )
    return ProductObservation(best.name, state, best.price, best.method, conflicting)


def _jsonld_observations(html: str) -> list[ProductObservation]:
    soup = BeautifulSoup(html, "html.parser")
    observations = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        observations.extend(_product_observations_from_json(data, DetectionMethod.JSON_LD))
    return observations


def _embedded_json_observations(html: str) -> list[ProductObservation]:
    soup = BeautifulSoup(html, "html.parser")
    observations = []
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        if re.search(r"ld\+json", str(script.get("type", "")), re.I):
            continue
        content = script.string or script.get_text()
        if not content or len(content) > 1_000_000:
            continue
        data_values = []
        try:
            data_values.append(json.loads(content))
        except json.JSONDecodeError:
            pass
        if not data_values:
            for match in re.finditer(r"(?:__\w+|INITIAL_STATE|initialState)\s*=\s*", content):
                start = content.find("{", match.end())
                if start < 0:
                    start = content.find("[", match.end())
                if start < 0:
                    continue
                try:
                    data, _ = decoder.raw_decode(content[start:])
                except json.JSONDecodeError:
                    continue
                data_values.append(data)
        for data in data_values:
            observations.extend(
                _product_observations_from_json(data, DetectionMethod.EMBEDDED_JSON)
            )
    return observations


def _html_observations(html: str, method: DetectionMethod = DetectionMethod.HTML) -> list[ProductObservation]:
    soup = BeautifulSoup(html, "html.parser")
    targets = []
    heading = soup.find("h1") or soup.find("title")
    product_title = " ".join(heading.stripped_strings) if heading else ""
    observations = []

    if product_title and ("playstation" in product_title.casefold() or "psn" in product_title.casefold()):
        for node in soup.find_all(True):
            node_text = re.sub(r"\s+", " ", " ".join(node.stripped_strings)).strip()
            if not node_text or len(node_text) > 500 or not TARGET_PRICE_PATTERN.search(node_text):
                continue

            current = node
            for _ in range(4):
                variant_text = re.sub(r"\s+", " ", " ".join(current.stripped_strings)).strip()
                if len(variant_text) > 500:
                    break
                if TARGET_PRICE_PATTERN.search(variant_text):
                    state = _state_from_text(variant_text)
                    disabled_controls = current.select(
                        "button[disabled], button[aria-disabled='true'], [data-available='false']"
                    )
                    if disabled_controls and state is StockState.UNKNOWN:
                        state = StockState.OUT_OF_STOCK
                    if state is not StockState.UNKNOWN:
                        price_match = PRICE_PATTERN.search(variant_text)
                        price = _format_price(price_match.group(1)) if price_match else "₹1,000"
                        observations.append(
                            ProductObservation(
                                name=f"{product_title} ₹1,000",
                                state=state,
                                price=price,
                                method=method,
                            )
                        )
                        break
                if current.parent is None or current.parent.name in {"body", "html"}:
                    break
                current = current.parent

    for node in soup.find_all(True):
        text = re.sub(r"\s+", " ", " ".join(node.stripped_strings)).strip()
        if text and len(text) <= 1000 and _is_target_product(text):
            targets.append((len(text), node, text))

    for _, target_node, product_name in sorted(targets, key=lambda item: item[0]):
        current = target_node
        for _ in range(7):
            text = " ".join(current.stripped_strings)
            state = _state_from_text(text)
            disabled_controls = current.select("button[disabled], button[aria-disabled='true'], [data-available='false']")
            if disabled_controls and state is StockState.UNKNOWN:
                state = StockState.OUT_OF_STOCK
            if state is not StockState.UNKNOWN or current.parent is None:
                price = None
                for price_node in current.select("[itemprop='price'], [data-price], .price, [class*='price']"):
                    value = price_node.get("content") or price_node.get("data-price") or price_node.get_text(" ", strip=True)
                    price = _format_price(value)
                    if price:
                        break
                if price is None:
                    price_match = PRICE_PATTERN.search(text)
                    if price_match:
                        price = _format_price(price_match.group(1))
                observations.append(ProductObservation(product_name, state, price, method))
                break
            if current.parent.name in {"body", "html"}:
                break
            current = current.parent
    return observations


def classify_page(html: str) -> tuple[StockState, str]:
    observation = _choose_observation(_html_observations(html))
    if observation is None:
        return StockState.UNKNOWN, "The exact ₹1,000 PlayStation product was not found."
    if observation.state is StockState.IN_STOCK:
        return observation.state, "The product section indicates it can be purchased."
    if observation.state is StockState.OUT_OF_STOCK:
        return observation.state, "The product section indicates it is unavailable."
    return observation.state, "The product was found, but stock evidence is missing or conflicting."


def discover_public_api_urls(store: Store, html: str) -> list[str]:
    candidates = [store.api_url] if store.api_url else []
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", href=True):
        link_type = str(link.get("type", "")).casefold()
        rel = " ".join(link.get("rel", [])).casefold()
        if "json" in link_type and ("alternate" in rel or "api" in rel):
            candidates.append(urljoin(store.url, link["href"]))

    page_text = html.casefold()
    parsed_url = urlsplit(store.url)
    origin = urlunsplit((parsed_url.scheme, parsed_url.netloc, "", "", ""))
    if any(marker in page_text for marker in ("cdn.shopify.com", "shopify.theme", "shopifyanalytics")):
        product_match = re.search(r"/products/([^/?#]+)", parsed_url.path)
        if product_match:
            candidates.append(
                urljoin(origin, f"/products/{product_match.group(1)}.js")
            )
        else:
            candidates.append(urljoin(origin, "/products.json?limit=250"))

    if any(marker in page_text for marker in ("woocommerce", "wc-blocks", "wc-store-api")):
        candidates.append(
            urljoin(origin, "/wp-json/wc/store/v1/products?search=PlayStation&per_page=100")
        )
    return list(dict.fromkeys(candidates))


class BrowserRenderer:
    def __init__(self) -> None:
        self._manager = None
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(1)

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None:
                return self._browser
            from playwright.async_api import async_playwright

            self._manager = async_playwright()
            self._playwright = await self._manager.start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser

    async def fetch(self, url: str) -> str | None:
        try:
            async with self._semaphore:
                browser = await self._ensure_browser()
                page = await browser.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1000)
                    return await page.content()
                finally:
                    await page.close()
        except Exception as error:
            logger.warning("Browser fallback failed for %s: %s", url, type(error).__name__)
            return None

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def _store_result(store: Store, observation: ProductObservation | None, detail: str) -> StoreResult:
    if observation is None:
        return StoreResult(store, StockState.UNKNOWN, detail)
    if observation.state is StockState.UNKNOWN:
        reason = "conflicting" if observation.conflicting else "missing"
        detail = f"Target product found, but stock evidence is {reason} ({observation.method.value})."
    else:
        detail = f"Detected by {observation.method.value}."
    return StoreResult(
        store=store,
        state=observation.state,
        detail=detail,
        product_name=observation.name[:120],
        price=observation.price,
        method=observation.method,
    )


async def _fetch_api_observations(
    session: aiohttp.ClientSession, url: str
) -> list[ProductObservation]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status >= 400:
                return []
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError):
        return []
    return _product_observations_from_json(payload, DetectionMethod.API)


async def check_store(
    session: aiohttp.ClientSession,
    store: Store,
    browser: BrowserRenderer | None = None,
) -> StoreResult:
    html = None
    request_detail = "The store page could not be retrieved."
    try:
        async with session.get(store.url) as response:
            if response.status >= 400:
                request_detail = f"HTTP {response.status}"
            else:
                html = await response.text(errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        request_detail = f"Request failed: {type(error).__name__}"

    unresolved = []
    api_urls = discover_public_api_urls(store, html or "")
    for api_url in api_urls:
        observation = _choose_observation(await _fetch_api_observations(session, api_url))
        if observation is not None:
            if observation.state is not StockState.UNKNOWN:
                return _store_result(store, observation, "")
            if observation.conflicting:
                return _store_result(store, observation, "")
            unresolved.append(observation)

    if html:
        jsonld = _choose_observation(_jsonld_observations(html))
        if jsonld is not None:
            if jsonld.state is not StockState.UNKNOWN:
                return _store_result(store, jsonld, "")
            if jsonld.conflicting:
                return _store_result(store, jsonld, "")
            unresolved.append(jsonld)

        embedded = _choose_observation(_embedded_json_observations(html))
        if embedded is not None:
            if embedded.state is not StockState.UNKNOWN:
                return _store_result(store, embedded, "")
            if embedded.conflicting:
                return _store_result(store, embedded, "")
            unresolved.append(embedded)

    rendered_html = await browser.fetch(store.url) if browser is not None else None
    if rendered_html:
        rendered_candidates = []
        rendered_candidates.extend(_jsonld_observations(rendered_html))
        rendered_candidates.extend(_embedded_json_observations(rendered_html))
        rendered_candidates.extend(_html_observations(rendered_html, DetectionMethod.BROWSER))
        rendered = _choose_observation(rendered_candidates)
        if rendered is not None:
            if rendered.state is not StockState.UNKNOWN:
                return _store_result(store, rendered, "")
            if rendered.conflicting:
                return _store_result(store, rendered, "")
            unresolved.append(rendered)

    if html:
        static_html = _choose_observation(_html_observations(html))
        if static_html is not None:
            if static_html.state is not StockState.UNKNOWN:
                return _store_result(store, static_html, "")
            if static_html.conflicting:
                return _store_result(store, static_html, "")
            unresolved.append(static_html)

    if unresolved:
        return _store_result(
            store,
            unresolved[-1],
            "The product was found, but stock evidence is missing or conflicting.",
        )
    return _store_result(store, None, request_detail if html is None else "Exact target product not found.")


async def check_all_stores() -> list[StoreResult]:
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": "PSNStockTracker/1.0 (personal stock monitoring bot)"}
    browser = BrowserRenderer()
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            return await asyncio.gather(
                *(check_store(session, store, browser) for store in STORES)
            )
    finally:
        await browser.close()


def should_alert(previous: StockState | None, current: StockState) -> bool:
    return current is StockState.IN_STOCK and previous is not StockState.IN_STOCK


def format_report(results: list[StoreResult]) -> str:
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"PSN ₹1,000 stock status | {checked_at}"]
    for result in results:
        details = [result.state.value]
        if result.product_name:
            details.append(result.product_name)
        if result.price:
            details.append(f"price {result.price}")
        if result.method:
            details.append(f"via {result.method.value}")
        details.append(result.detail)
        lines.append(
            f"- [{result.store.name}]({result.store.url}): "
            + " | ".join(details)
        )
    return "\n".join(lines)


def format_stock_alert(
    store: Store, product_name: str | None = None, price: str | None = None
) -> str:
    product = f" ({product_name})" if product_name else ""
    price_detail = f" Listed price: {price}." if price else ""
    return (
        f"Stock detected: {store.name}{product} may have the PSN ₹1,000 wallet top-up in stock."
        f"{price_detail}\n"
        f"[Open {store.name}]({store.url})"
    )


def format_help() -> str:
    return (
        "**PSN Stock Tracker commands**\n"
        "`!ping` - Check whether the bot and store polling are running.\n"
        "`!status` - Check all stores and get clickable product links (admin only).\n"
        "`/status` - Same on-demand store report as a slash command (admin only).\n"
        "`!help` - Show this command list."
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
        if message.author.bot:
            return

        command = message.content.strip().casefold()
        if command == "!ping":
            await message.channel.send(
                format_ping(self.last_check_at, self.poll_stores.is_running())
            )
        elif command == "!help":
            await message.channel.send(format_help())
        elif command == "!status":
            if message.author.id != self.admin_user_id:
                await message.channel.send("You are not authorized to use this command.")
                return

            status_message = await message.channel.send("Checking all stores...")
            results = await check_all_stores()
            await status_message.edit(content=format_report(results))

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
                    await channel.send(
                        format_stock_alert(result.store, result.product_name, result.price)
                    )
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