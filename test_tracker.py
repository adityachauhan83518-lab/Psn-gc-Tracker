import unittest
from datetime import datetime, timezone

from tracker import (
    STORES,
    DetectionMethod,
    StockState,
    Store,
    StoreResult,
    _choose_observation,
    _embedded_json_observations,
    _html_observations,
    _jsonld_observations,
    _state_from_text,
    classify_page,
    check_store,
    discover_public_api_urls,
    format_help,
    format_ping,
    format_report,
    format_stock_alert,
    should_alert,
)


class FakeResponse:
    def __init__(self, body="", payload=None, status=200):
        self.body = body
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def text(self, errors="strict"):
        return self.body

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.get(url, FakeResponse(status=404))


class FakeBrowser:
    def __init__(self, html):
        self.html = html
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return self.html


class ClassifyPageTests(unittest.TestCase):
    def test_reports_available_target_product(self):
        html = "<article><h2>PlayStation Network Wallet Top-Up ₹1,000</h2><button>Add to cart</button></article>"
        self.assertEqual(classify_page(html)[0], StockState.IN_STOCK)

    def test_reports_unavailable_target_product(self):
        html = "<article><h2>PSN Wallet ₹1,000</h2><p>Sold out</p></article>"
        self.assertEqual(classify_page(html)[0], StockState.OUT_OF_STOCK)

    def test_keeps_unmatched_product_unknown(self):
        html = "<article><h2>PlayStation Wallet ₹500</h2><button>Buy now</button></article>"
        self.assertEqual(classify_page(html)[0], StockState.UNKNOWN)

    def test_keeps_conflicting_signals_unknown(self):
        html = "<article><h2>PSN Wallet ₹1,000</h2><p>Sold out</p><button>Buy now</button></article>"
        self.assertEqual(classify_page(html)[0], StockState.UNKNOWN)

    def test_not_available_is_not_misread_as_in_stock(self):
        html = "<article><h2>PSN Wallet 1,000</h2><p>Not available</p></article>"
        self.assertEqual(classify_page(html)[0], StockState.OUT_OF_STOCK)

    def test_variant_stock_is_scoped_to_exact_1000_card(self):
        html = (
            "<h1>Playstation (PSN) Gift Card - INDIA</h1>"
            "<p>1 = ₹1 cashback</p>"
            "<a>SOLD OUT <span>₹1,000</span><span>Coupon Worth ₹1000</span></a>"
            "<a><button>Add to cart</button><span>₹2,000</span></a>"
        )
        observation = _choose_observation(_html_observations(html))
        self.assertEqual(observation.state, StockState.OUT_OF_STOCK)
        self.assertEqual(observation.price, "₹1,000")

    def test_json_ld_extracts_product_stock_and_price(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"PlayStation Network Wallet Top-Up ₹1,000",'
            '"offers":{"@type":"Offer","availability":"https://schema.org/InStock",'
            '"price":"1000","priceCurrency":"INR"}}'
            '</script>'
        )
        observations = _jsonld_observations(html)
        observation = _choose_observation(observations)
        self.assertEqual(observation.state, StockState.IN_STOCK)
        self.assertEqual(observation.price, "₹1,000")
        self.assertEqual(observation.method, DetectionMethod.JSON_LD)

    def test_embedded_json_extracts_product_stock_and_price(self):
        html = (
            '<script>window.__INITIAL_STATE__ = '
            '{"product":{"name":"PSN Wallet 1,000","available":true,"price":1000}};'
            '</script>'
        )
        observations = _embedded_json_observations(html)
        self.assertEqual(observations[0].state, StockState.IN_STOCK)
        self.assertEqual(observations[0].price, "₹1,000")
        self.assertEqual(observations[0].method, DetectionMethod.EMBEDDED_JSON)

    def test_woocommerce_minor_unit_price_is_scaled(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"PSN Wallet 1,000",'
            '"offers":{"availability":"https://schema.org/InStock",'
            '"prices":{"price":"100000","currency_code":"INR",'
            '"currency_minor_unit":2}}}'
            '</script>'
        )
        observation = _choose_observation(_jsonld_observations(html))
        self.assertEqual(observation.price, "₹1,000")

    def test_conflicting_structured_stock_signals_are_unknown(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"PSN Wallet 1,000",'
            '"offers":{"availability":"https://schema.org/OutOfStock"},'
            '"available":true}'
            '</script>'
        )
        self.assertEqual(
            _choose_observation(_jsonld_observations(html)).state,
            StockState.UNKNOWN,
        )


class DetectionFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_api_is_preferred_to_html(self):
        store = Store("Example", "https://shop.example/wallet", "https://shop.example/api")
        session = FakeSession(
            {
                store.url: FakeResponse(body="<h1>PSN Wallet 1,000</h1>"),
                store.api_url: FakeResponse(
                    payload={
                        "product": {
                            "name": "PSN Wallet 1,000",
                            "available": False,
                            "price": 100000,
                        }
                    }
                ),
            }
        )

        result = await check_store(session, store)

        self.assertEqual(result.state, StockState.OUT_OF_STOCK)
        self.assertEqual(result.price, "₹1,000")
        self.assertEqual(result.method, DetectionMethod.API)
        self.assertEqual(session.calls, [store.url, store.api_url])

    async def test_conflicting_api_stock_does_not_fall_through_to_positive_html(self):
        store = Store("Example", "https://shop.example/wallet", "https://shop.example/api")
        session = FakeSession(
            {
                store.url: FakeResponse(
                    body="<article><h1>PSN Wallet 1,000</h1><button>Add to cart</button></article>"
                ),
                store.api_url: FakeResponse(
                    payload={
                        "product": {
                            "name": "PSN Wallet 1,000",
                            "available": True,
                            "status": "Sold out",
                        }
                    }
                ),
            }
        )
        browser = FakeBrowser("<h1>PSN Wallet 1,000</h1><button>Buy now</button>")

        result = await check_store(session, store, browser)

        self.assertEqual(result.state, StockState.UNKNOWN)
        self.assertEqual(result.method, DetectionMethod.API)
        self.assertEqual(browser.calls, [])

    async def test_browser_fallback_reads_javascript_rendered_stock(self):
        store = Store("Example", "https://shop.example/wallet")
        session = FakeSession({store.url: FakeResponse(body="<main>Loading...</main>")})
        browser = FakeBrowser(
            "<article><h1>PlayStation Wallet 1,000</h1><button>Add to cart</button></article>"
        )

        result = await check_store(session, store, browser)

        self.assertEqual(result.state, StockState.IN_STOCK)
        self.assertEqual(result.method, DetectionMethod.BROWSER)
        self.assertEqual(browser.calls, [store.url])

    def test_shopify_public_json_endpoint_is_discovered(self):
        store = Store("Example", "https://shop.example/products/psn-wallet-1000")
        urls = discover_public_api_urls(store, "<script src='cdn.shopify.com/theme.js'></script>")
        self.assertIn("https://shop.example/products/psn-wallet-1000.js", urls)

    def test_woocommerce_public_api_endpoint_is_discovered(self):
        store = Store("Example", "https://shop.example/playstation")
        urls = discover_public_api_urls(store, "<body class='woocommerce wc-store-api'>")
        self.assertTrue(any("/wp-json/wc/store/v1/products" in url for url in urls))


class AlertTransitionTests(unittest.TestCase):
    def test_alerts_when_product_becomes_available(self):
        self.assertTrue(should_alert(StockState.OUT_OF_STOCK, StockState.IN_STOCK))

    def test_stock_alert_contains_clickable_store_link(self):
        message = format_stock_alert(STORES[0])
        self.assertIn(f"[Open {STORES[0].name}]({STORES[0].url})", message)


class StatusReportTests(unittest.TestCase):
    def test_report_links_all_eight_stores(self):
        results = [
            StoreResult(store, StockState.UNKNOWN, "Not checked")
            for store in STORES
        ]
        report = format_report(results)
        for store in STORES:
            with self.subTest(store=store.name):
                self.assertIn(f"[{store.name}]({store.url})", report)

    def test_report_shows_price_and_detection_method(self):
        result = StoreResult(
            STORES[0],
            StockState.IN_STOCK,
            "Detected by JSON-LD.",
            product_name="PSN Wallet 1,000",
            price="₹1,000",
            method=DetectionMethod.JSON_LD,
        )
        report = format_report([result])
        self.assertIn("price ₹1,000", report)
        self.assertIn("via JSON-LD", report)

    def test_help_lists_available_commands(self):
        help_message = format_help()
        for command in ("!ping", "!status", "/status", "!help"):
            with self.subTest(command=command):
                self.assertIn(f"`{command}`", help_message)


class PingMessageTests(unittest.TestCase):
    def test_reports_when_polling_has_not_started(self):
        self.assertEqual(
            format_ping(None, False),
            "I'm alive, but store polling is not running.",
        )

    def test_reports_first_scan_pending(self):
        self.assertIn("first store scan hasn't completed", format_ping(None, True))

    def test_reports_last_completed_scan(self):
        checked_at = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)
        message = format_ping(checked_at, True)
        self.assertIn("polling every 30 seconds", message)
        self.assertIn("2026-10-22 12:30:00 UTC", message)

    def test_does_not_repeat_while_available(self):
        self.assertFalse(should_alert(StockState.IN_STOCK, StockState.IN_STOCK))

    def test_alerts_again_after_stock_returns(self):
        self.assertTrue(should_alert(StockState.OUT_OF_STOCK, StockState.IN_STOCK))


if __name__ == "__main__":
    unittest.main()