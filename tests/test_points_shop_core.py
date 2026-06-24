import asyncio
import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path


def install_stub_modules():
    astrbot = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    logger_module = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )

    class DummyFilter:
        class EventMessageType:
            GROUP_MESSAGE = "group"

        @staticmethod
        def command(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class DummyStar:
        def __init__(self, context):
            self.context = context

    class DummyStarTools:
        data_dir = Path(tempfile.mkdtemp(prefix="points-shop-stubs-"))

        @staticmethod
        def get_data_dir(name):
            target = DummyStarTools.data_dir / name
            target.mkdir(parents=True, exist_ok=True)
            return target

    def register(*args, **kwargs):
        def decorator(obj):
            return obj

        return decorator

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = DummyFilter

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = DummyStar
    star_module.StarTools = DummyStarTools
    star_module.register = register

    message_components_module = types.ModuleType("astrbot.api.message_components")
    message_components_module.Plain = lambda text="": ("plain", text)
    message_components_module.Image = lambda file="": ("image", file)

    class DummyMessageChain:
        def __init__(self, chain=None):
            self.ops = list(chain or [])

        def at_all(self):
            self.ops.append(("at_all", "all"))
            return self

        def message(self, text):
            self.ops.append(("message", text))
            return self

    message_result_module = types.ModuleType("astrbot.core.message.message_event_result")
    message_result_module.MessageChain = DummyMessageChain

    quart_module = types.ModuleType("quart")

    class RequestStub:
        next_json = {}

        @staticmethod
        async def get_json(silent=True):
            return RequestStub.next_json

    quart_module.request = RequestStub
    quart_module.jsonify = lambda payload: payload
    quart_module.send_file = lambda *args, **kwargs: {"sent_file": True}

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.logger"] = logger_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.api.message_components"] = message_components_module
    sys.modules["astrbot.core.message.message_event_result"] = message_result_module
    sys.modules["quart"] = quart_module

    api_module.logger = logger_module
    api_module.message_components = message_components_module

    return DummyStarTools, RequestStub


class DummyContext:
    def __init__(self):
        self.routes = []
        self.sent_messages = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, tuple(methods), desc))

    async def send_message(self, group_sid, chain):
        self.sent_messages.append((group_sid, chain))
        return True


class DummyEvent:
    def __init__(self, user_id="u1", user_name="用户1", group_id="100", platform_id="qq", is_admin=False):
        self._user_id = str(user_id)
        self._user_name = user_name
        self._group_id = str(group_id)
        self._platform_id = platform_id
        self._is_admin = is_admin
        self.unified_msg_origin = f"{platform_id}:GroupMessage:{group_id}"
        self.message_obj = types.SimpleNamespace(
            sender={"user_id": self._user_id, "nickname": self._user_name},
            group_id=self._group_id,
            message_id=f"mid-{self._user_id}-{self._group_id}",
        )
        self.raw_message = {"group_name": "测试群"}
        self.message_str = ""

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._user_name

    def get_platform_id(self):
        return self._platform_id

    def is_admin(self):
        return self._is_admin


STAR_TOOLS, REQUEST_STUB = install_stub_modules()
MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
MODULE_NAME = "points_shop_main_under_test"
if MODULE_NAME in sys.modules:
    del sys.modules[MODULE_NAME]
spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
points_shop_module = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = points_shop_module
assert spec and spec.loader
spec.loader.exec_module(points_shop_module)
PointsShopPlugin = points_shop_module.PointsShopPlugin


class PointsShopCoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = Path(tempfile.mkdtemp(prefix="points-shop-test-"))
        STAR_TOOLS.data_dir = self.tempdir

    def tearDown(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def make_plugin(self, config=None):
        plugin = PointsShopPlugin(DummyContext(), config or {})
        plugin.state = plugin._default_state()
        return plugin

    def test_rps_segment_rates_support_override_and_clear(self):
        plugin = self.make_plugin(
            {
                "rps_win_rate": 33,
                "rps_segment_win_rates": "1-5:99",
                "rps_win_rate_segments": [{"min": 1, "max": 5, "win_rate": 88}],
            }
        )

        plugin.state["admin_settings"] = plugin._normalize_admin_settings_state(
            {"rps_segment_win_rates": "1-5:60\n6-10:45\n11-50:35\n51-*:20"}
        )
        self.assertEqual(plugin._rps_segments()[0]["win_rate"], 60)
        self.assertEqual(plugin._rps_win_rate_for_score(5), 60)
        self.assertEqual(plugin._rps_win_rate_for_score(10), 45)
        self.assertEqual(plugin._rps_win_rate_for_score(25), 35)
        self.assertEqual(plugin._rps_win_rate_for_score(88), 20)

        plugin.state["admin_settings"] = plugin._normalize_admin_settings_state({"rps_segment_win_rates": ""})
        self.assertEqual(plugin._rps_segments(), [])
        self.assertEqual(plugin._rps_win_rate_for_score(5), 33)

    def test_lottery_one_bet_per_user_per_issue_and_draw(self):
        plugin = self.make_plugin()
        plugin.state["admin_settings"] = plugin._normalize_admin_settings_state(
            {"lottery_min_bet": 1, "lottery_max_bet": 100, "lottery_multiplier": 8}
        )

        event = DummyEvent(user_id="u1", user_name="张三")
        issue, user_bets = plugin._place_lottery_bet(event, 5, 10)
        self.assertEqual(issue, 1)
        self.assertEqual(len(user_bets), 1)
        self.assertEqual(user_bets[0]["stake"], 10)
        current_issue_bets = plugin._lottery_bets(issue)
        user_bet = next((bet for bet in current_issue_bets if bet["user_id"] == "u1"), None)
        self.assertIsNotNone(user_bet)
        self.assertEqual(user_bet["number"], 5)
        self.assertEqual(user_bet["stake"], 10)
        stats = plugin._lottery_number_stats(current_issue_bets)
        self.assertEqual(stats[5]["users"], 1)
        self.assertEqual(stats[5]["stake"], 10)
        self.assertEqual(stats[1]["users"], 0)

        plugin._set_lottery_forced_number(5)
        result = plugin._draw_lottery()
        self.assertEqual(result["draw_number"], 5)
        self.assertEqual(result["winner_count"], 1)
        self.assertEqual(result["reward_total"], 80)
        self.assertEqual(plugin._balance("qq:GroupMessage:100", "u1"), 80)
        self.assertEqual(plugin._lottery_issue(), 2)
        self.assertIsNone(plugin._lottery_current_forced_number())
        self.assertEqual(plugin._lottery_bets(1), [])

    def test_notice_chain_and_notice_api_flow(self):
        plugin = self.make_plugin()
        group_sid = "qq:GroupMessage:100"
        plugin.state["known_groups"] = {
            group_sid: {"platform_id": "qq", "group_id": "100", "group_name": "测试群"}
        }

        chain = plugin._build_notice_chain({"content": "补货提醒", "at_all": True})
        self.assertEqual(chain.ops[0], ("at_all", "all"))
        self.assertEqual(chain.ops[1], ("message", "\n补货提醒"))

        REQUEST_STUB.next_json = {
            "name": "晚间通知",
            "interval_minutes": 30,
            "content": "今晚开奖记得来",
            "group_sids": [group_sid],
            "enabled": True,
            "at_all": True,
        }
        response = asyncio.run(plugin.api_admin_notice_save())
        self.assertEqual(response["status"], "ok")
        job = response["data"]["job"]
        self.assertTrue(job["id"])
        self.assertEqual(len(response["data"]["notices"]["jobs"]), 1)

        REQUEST_STUB.next_json = {"id": job["id"]}
        send_response = asyncio.run(plugin.api_admin_notice_send())
        self.assertEqual(send_response["status"], "ok")
        self.assertTrue(send_response["data"]["sent"])
        self.assertEqual(plugin.context.sent_messages[0][0], group_sid)

    def test_settings_api_preserves_zero_values(self):
        plugin = self.make_plugin({"rps_win_rate": 33, "rps_draw_rate": 33})
        REQUEST_STUB.next_json = {
            "rps": {
                "default_win_rate": 0,
                "draw_rate": 0,
                "segments_text": "1-5:60",
            },
            "lottery": {
                "min_bet": 5,
                "max_bet": 10,
                "multiplier": 3,
                "forced_number": "",
            },
        }
        response = asyncio.run(plugin.api_admin_settings_save())
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["rps"]["default_win_rate"], 0)
        self.assertEqual(response["data"]["rps"]["draw_rate"], 0)
        self.assertEqual(response["data"]["lottery"]["min_bet"], 5)
        self.assertEqual(response["data"]["lottery"]["max_bet"], 10)
        self.assertEqual(response["data"]["lottery"]["multiplier"], 3)
        self.assertIsNone(response["data"]["lottery"]["forced_number"])


if __name__ == "__main__":
    unittest.main()
