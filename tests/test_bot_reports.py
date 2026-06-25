import tempfile
import unittest
import os
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

requests = types.ModuleType("requests")
requests.__path__ = []
requests.get = lambda *args, **kwargs: None
requests.post = lambda *args, **kwargs: None
packages = types.ModuleType("requests.packages")
packages.__path__ = []
urllib3 = types.ModuleType("requests.packages.urllib3")
urllib3.disable_warnings = lambda *args, **kwargs: None
urllib3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
requests.packages = packages
packages.urllib3 = urllib3
sys.modules.setdefault("requests", requests)
sys.modules.setdefault("requests.packages", packages)
sys.modules.setdefault("requests.packages.urllib3", urllib3)

dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv)
os.environ.setdefault("MAX_BOT_TOKEN", "test-token")

import bot
from reports import MOSCOW_TZ, ComplaintStore


class BotReportFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        bot.STORE = ComplaintStore(Path(self.temp_directory.name) / "complaints.db")
        bot.user_states.clear()
        bot.user_chat_id.clear()
        bot.report_periods.clear()

    def tearDown(self):
        self.temp_directory.cleanup()

    @patch("bot.send_message")
    def test_password_and_today_report(self, send_message):
        user_id = 10
        chat_id = 20
        bot.begin_report(user_id, chat_id)
        self.assertEqual(bot.user_states[user_id], bot.STATE_REPORT_PASSWORD)

        bot.handle_report_input(user_id, bot.REPORT_PASSWORD)
        self.assertEqual(bot.user_states[user_id], bot.STATE_REPORT_MENU)

        timestamp = datetime.now(MOSCOW_TZ)
        bot.STORE.add(1, "Иван", "Описание", "Адрес АЗС", timestamp)
        bot.handle_report_input(user_id, "/report_today")

        self.assertIn(user_id, bot.report_periods)
        summary = send_message.call_args_list[-1].args[1]
        self.assertIn("Всего зафиксировано нарушений: 1", summary)
        self.assertIn("Адрес АЗС — 1", summary)

    @patch("bot.send_message")
    def test_custom_period_validation(self, send_message):
        user_id = 10
        bot.user_chat_id[user_id] = 20
        bot.user_states[user_id] = bot.STATE_REPORT_MENU

        bot.handle_report_input(user_id, "/report_custom")
        self.assertEqual(bot.user_states[user_id], bot.STATE_REPORT_CUSTOM)

        bot.handle_report_input(user_id, "не дата")
        self.assertIn("Используйте формат", send_message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
