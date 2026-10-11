import unittest

from gui.navigation_model import NAV_ITEMS, DEFAULT_PAGE, enabled_nav_keys


class GuiNavigationModelTests(unittest.TestCase):
    def test_default_page_is_dashboard(self):
        self.assertEqual(DEFAULT_PAGE, "dashboard")

    def test_core_pages_are_enabled(self):
        enabled = set(enabled_nav_keys())
        self.assertIn("dashboard", enabled)
        self.assertIn("forward", enabled)
        self.assertIn("execution", enabled)

    def test_future_pages_are_reserved_but_disabled(self):
        items = {x["key"]: x for x in NAV_ITEMS}
        self.assertFalse(items["strategies"]["enabled"])
        self.assertFalse(items["portfolio"]["enabled"])
        self.assertFalse(items["system"]["enabled"])

    def test_execution_is_under_trading_group(self):
        items = {x["key"]: x for x in NAV_ITEMS}
        self.assertEqual(items["execution"]["group"], "TRADING")


if __name__ == "__main__":
    unittest.main()
