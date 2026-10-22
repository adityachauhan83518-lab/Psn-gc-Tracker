import unittest
from datetime import datetime, timezone

from tracker import (
    STORES,
    StockState,
    StoreResult,
    classify_page,
    format_help,
    format_ping,
    format_report,
    format_stock_alert,
    should_alert,
)


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