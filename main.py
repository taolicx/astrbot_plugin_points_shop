from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover - runtime dependency guard
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None


PLUGIN_NAME = "astrbot_plugin_points_shop"
PLUGIN_VERSION = "0.1.2"
GROUP_MESSAGE_TYPE = "GroupMessage"

MOVE_ALIASES = {
    "石头": "rock",
    "拳头": "rock",
    "rock": "rock",
    "r": "rock",
    "剪刀": "scissors",
    "scissor": "scissors",
    "scissors": "scissors",
    "s": "scissors",
    "布": "paper",
    "包袱": "paper",
    "paper": "paper",
    "p": "paper",
}
MOVE_LABELS = {
    "rock": "石头",
    "scissors": "剪刀",
    "paper": "布",
}
MOVE_ICONS = {
    "rock": "✊",
    "scissors": "✌",
    "paper": "✋",
}
WIN_MAP = {
    "rock": "scissors",
    "scissors": "paper",
    "paper": "rock",
}
LOSE_MAP = {loser: winner for winner, loser in WIN_MAP.items()}


@register(
    PLUGIN_NAME,
    "codex",
    "群聊积分兑换系统：签到领积分、猜拳下注、积分兑换商品，并生成精美商品展示图。",
    PLUGIN_VERSION,
)
class PointsShopPlugin(Star):
    def __init__(self, context: Context, config: Any):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_dir = Path(__file__).resolve().parent
        self.state_path = self._resolve_path(self._cfg_str("state_path", "data/state.json"))
        self.poster_path = self._resolve_path(self._cfg_str("poster_path", "data/shop_poster.png"))
        self.state: dict[str, Any] = self._default_state()
        self._lock = asyncio.Lock()

    async def initialize(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.poster_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._load_state)
        await asyncio.to_thread(self._sync_stock_from_config)
        await asyncio.to_thread(self._save_state)
        logger.info("[PointsShop] initialized")

    async def terminate(self):
        await asyncio.to_thread(self._save_state)
        logger.info("[PointsShop] terminated")

    @filter.command("签到", alias={"打卡", "每日签到"}, priority=100)
    async def sign_in(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "签到需要在群聊里进行。")
            return

        async with self._lock:
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            self._remember_user(event)

            today = datetime.now().strftime("%Y-%m-%d")
            last_day = self.state.setdefault("signins", {}).setdefault(group_sid, {}).get(user_id)
            if last_day == today:
                balance = self._balance(group_sid, user_id)
                await self._reply_and_stop(event, f"今天已经签到过啦。\n当前积分：{balance}")
                return

            min_reward = max(0, self._cfg_int("signin_min_points", 8))
            max_reward = max(min_reward, self._cfg_int("signin_max_points", 18))
            reward = random.randint(min_reward, max_reward)
            streak = self._next_streak(group_sid, user_id, today)
            bonus = self._streak_bonus(streak)
            total = reward + bonus

            self.state["signins"][group_sid][user_id] = today
            self._set_streak(group_sid, user_id, streak, today)
            balance = self._add_points(group_sid, user_id, total)
            self._save_state()

        bonus_text = f"\n连续签到奖励：+{bonus}" if bonus else ""
        await self._reply_and_stop(
            event,
            f"签到成功，获得 {total} 积分！{bonus_text}\n连续签到：{streak} 天\n当前积分：{balance}",
        )

    @filter.command("积分", alias={"我的积分", "余额"}, priority=100)
    async def show_points(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "积分按群聊分别统计，请在群聊里查看。")
            return

        async with self._lock:
            self._remember_user(event)
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            balance = self._balance(group_sid, user_id)
            streak = self.state.setdefault("streaks", {}).setdefault(group_sid, {}).get(user_id, {}).get("days", 0)

        await self._reply_and_stop(event, f"{self._sender_name(event)}\n当前积分：{balance}\n连续签到：{streak} 天")

    @filter.command("积分排行", alias={"排行榜", "积分榜"}, priority=100)
    async def leaderboard(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "积分排行需要在群聊里查看。")
            return

        group_sid = self._group_sid(event)
        async with self._lock:
            balances = dict(self.state.setdefault("balances", {}).get(group_sid, {}))
            profiles = self.state.setdefault("profiles", {}).get(group_sid, {})

        if not balances:
            await self._reply_and_stop(event, "本群还没有积分记录，先发 /签到 开始积累吧。")
            return

        limit = max(3, min(20, self._cfg_int("leaderboard_limit", 10)))
        rows = sorted(balances.items(), key=lambda item: int(item[1] or 0), reverse=True)[:limit]
        lines = ["本群积分排行："]
        for index, (user_id, points) in enumerate(rows, start=1):
            name = str(profiles.get(user_id, {}).get("name") or user_id)
            lines.append(f"{index}. {name} - {int(points)}")
        await self._reply_and_stop(event, "\n".join(lines))

    @filter.command("猜拳", alias={"剪刀石头布", "石头剪刀布", "划拳"}, priority=100)
    async def rps(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "猜拳下注需要在群聊里进行。")
            return

        payload = self._command_payload(event, ("猜拳", "剪刀石头布", "石头剪刀布", "划拳"))
        move, bet = self._parse_rps_payload(payload)
        if not move or bet is None:
            await self._reply_and_stop(
                event,
                f"用法：/猜拳 <石头|剪刀|布> <积分>\n下注范围：{self._min_bet()}~{self._max_bet()} 积分",
            )
            return

        min_bet = self._min_bet()
        max_bet = self._max_bet()
        if bet < min_bet or bet > max_bet:
            await self._reply_and_stop(event, f"下注积分需要在 {min_bet}~{max_bet} 之间。")
            return

        async with self._lock:
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            self._remember_user(event)
            balance = self._balance(group_sid, user_id)
            if balance < bet:
                await self._reply_and_stop(event, f"积分不够下注。\n当前积分：{balance}\n本次需要：{bet}")
                return

            self._add_points(group_sid, user_id, -bet)
            bot_move = self._choose_rps_bot_move(move)
            if move == bot_move:
                self._add_points(group_sid, user_id, bet)
                result = "平局，本金已返还。"
                delta = 0
            elif WIN_MAP[move] == bot_move:
                reward = bet * 2
                self._add_points(group_sid, user_id, reward)
                result = f"你赢了，返还 {reward} 积分！"
                delta = bet
            else:
                result = "你输了，本次下注不返还。"
                delta = -bet

            new_balance = self._balance(group_sid, user_id)
            self._append_game_record(event, move, bot_move, bet, delta)
            self._save_state()

        await self._reply_and_stop(
            event,
            f"你出了 {MOVE_ICONS[move]} {MOVE_LABELS[move]}\n"
            f"我出了 {MOVE_ICONS[bot_move]} {MOVE_LABELS[bot_move]}\n"
            f"{result}\n当前积分：{new_balance}",
        )

    @filter.command("兑换商城", alias={"商店", "积分商城", "商品列表", "兑换列表"}, priority=100)
    async def shop(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "兑换商城需要在群聊里查看。")
            return

        async with self._lock:
            self._remember_user(event)
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            balance = self._balance(group_sid, user_id)
            items = self._items()
            stocks = dict(self.state.setdefault("stock", {}))

        if not items:
            await self._reply_and_stop(event, "兑换商城还没有配置商品，请先在插件设置里添加 items。")
            return

        if Image is None:
            await self._reply_and_stop(event, self._shop_text(items, stocks, balance))
            return

        try:
            await asyncio.to_thread(self._render_shop_poster, items, stocks, balance, self._sender_name(event))
            chain = [
                self._image_component(self.poster_path),
                Comp.Plain(text=f"\n当前积分：{balance}\n发送 /兑换 <商品ID或名称> [数量] 进行兑换。"),
            ]
            await event.send(MessageChain(chain))
            event.stop_event()
        except Exception as exc:
            logger.exception(f"[PointsShop] render shop poster failed: {exc}")
            await self._reply_and_stop(event, self._shop_text(items, stocks, balance))

    @filter.command("兑换", alias={"购买", "积分兑换"}, priority=100)
    async def exchange(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "兑换需要在群聊里进行。")
            return

        payload = self._command_payload(event, ("兑换", "购买", "积分兑换"))
        item_key, qty = self._parse_exchange_payload(payload)
        if not item_key:
            await self._reply_and_stop(event, "用法：/兑换 <商品ID或名称> [数量]\n发送 /商店 查看可兑换商品。")
            return

        qty = max(1, qty)
        async with self._lock:
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            self._remember_user(event)
            item = self._find_item(item_key)
            if not item:
                await self._reply_and_stop(event, "没有找到这个商品。发送 /商店 查看商品ID和名称。")
                return

            item_id = str(item.get("id") or "")
            price = max(0, int(item.get("price") or 0))
            cost = price * qty
            if cost <= 0:
                await self._reply_and_stop(event, "这个商品价格配置异常，请联系管理员检查插件设置。")
                return

            balance = self._balance(group_sid, user_id)
            if balance < cost:
                await self._reply_and_stop(event, f"积分不足，兑换失败。\n需要：{cost}\n当前：{balance}")
                return

            stock = self._stock_left(item)
            if stock >= 0 and stock < qty:
                await self._reply_and_stop(event, f"库存不足，当前剩余：{stock}")
                return

            self._add_points(group_sid, user_id, -cost)
            if stock >= 0:
                self.state.setdefault("stock", {})[item_id] = stock - qty
            record = self._append_exchange_record(event, item, qty, cost)
            new_balance = self._balance(group_sid, user_id)
            self._save_state()

        delivery = str(item.get("delivery") or "").strip()
        delivery_text = f"\n兑换说明：{delivery}" if delivery else ""
        await self._reply_and_stop(
            event,
            f"兑换成功！\n"
            f"商品：{item.get('name')} x{qty}\n"
            f"消耗：{cost} 积分\n"
            f"订单号：{record['order_id']}\n"
            f"剩余积分：{new_balance}{delivery_text}",
        )

    @filter.command("兑换记录", alias={"我的兑换", "订单"}, priority=100)
    async def exchange_records(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "兑换记录需要在群聊里查看。")
            return

        group_sid = self._group_sid(event)
        user_id = self._sender_id(event)
        async with self._lock:
            records = [
                item
                for item in self.state.setdefault("exchange_records", [])
                if item.get("group_sid") == group_sid and item.get("user_id") == user_id
            ][-5:]

        if not records:
            await self._reply_and_stop(event, "你还没有兑换记录。")
            return

        lines = ["最近兑换记录："]
        for item in reversed(records):
            lines.append(
                f"{item.get('time')} | {item.get('item_name')} x{item.get('qty')} | "
                f"{item.get('cost')}积分 | {item.get('order_id')}"
            )
        await self._reply_and_stop(event, "\n".join(lines))

    @filter.command("积分帮助", alias={"兑换帮助", "商店帮助"}, priority=100)
    async def help_text(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        await self._reply_and_stop(event, self._help_text())

    @filter.command("积分管理", alias={"积分调整"}, priority=100)
    async def manage_points(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._cfg_bool("enable_admin_adjust", True):
            return
        if not self._is_admin(event):
            await self._reply_and_stop(event, "只有管理员可以调整积分。")
            return

        payload = self._command_payload(event, ("积分管理", "积分调整"))
        target_id, delta = self._parse_adjust_payload(event, payload)
        if not target_id or delta is None:
            await self._reply_and_stop(event, "用法：/积分管理 <用户ID或@用户> <+/-积分>\n例：/积分管理 123456 +50")
            return

        async with self._lock:
            group_sid = self._group_sid(event)
            new_balance = self._add_points(group_sid, target_id, delta)
            self._save_state()

        await self._reply_and_stop(event, f"已调整积分：{target_id} {delta:+d}\n当前积分：{new_balance}")

    def _default_state(self) -> dict[str, Any]:
        return {
            "balances": {},
            "profiles": {},
            "signins": {},
            "streaks": {},
            "stock": {},
            "exchange_records": [],
            "game_records": [],
        }

    def _load_state(self) -> None:
        self.state = self._default_state()
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    self.state[key] = value
        except Exception as exc:
            logger.warning(f"[PointsShop] load state failed: {exc}")

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[PointsShop] save state failed: {exc}")

    def _sync_stock_from_config(self) -> None:
        stock_map = self.state.setdefault("stock", {})
        for item in self._items():
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            configured = int(item.get("stock", -1) if item.get("stock", -1) is not None else -1)
            if item_id not in stock_map:
                stock_map[item_id] = configured
            elif configured < 0:
                stock_map[item_id] = -1

    def _items(self) -> list[dict[str, Any]]:
        raw = self.config.get("items")
        if not isinstance(raw, list):
            raw = self._default_items()
        items: list[dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or index).strip()
            name = str(item.get("name") or f"商品{index}").strip()
            price = max(1, int(item.get("price") or 1))
            enabled = self._to_bool(item.get("enabled", True), True)
            if not item_id or not name or not enabled:
                continue
            items.append(
                {
                    "id": item_id,
                    "name": name,
                    "price": price,
                    "stock": int(item.get("stock", -1) if item.get("stock", -1) is not None else -1),
                    "description": str(item.get("description") or "").strip(),
                    "delivery": str(item.get("delivery") or "").strip(),
                    "emoji": str(item.get("emoji") or "🎁").strip()[:4],
                    "color": str(item.get("color") or "").strip(),
                    "enabled": True,
                }
            )
        return items

    def _default_items(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "coffee",
                "name": "今日咖啡",
                "price": 60,
                "stock": -1,
                "emoji": "☕",
                "description": "给自己兑换一杯精神补给",
                "delivery": "请联系管理员核销。",
                "color": "#6f4e37",
                "enabled": True,
            },
            {
                "id": "title",
                "name": "群头衔定制",
                "price": 180,
                "stock": 5,
                "emoji": "🏷",
                "description": "兑换一次群头衔/称号修改",
                "delivery": "兑换后把想要的头衔发给管理员。",
                "color": "#4f46e5",
                "enabled": True,
            },
            {
                "id": "mystery",
                "name": "神秘盲盒",
                "price": 300,
                "stock": 3,
                "emoji": "🎁",
                "description": "随机小奖励，开盒有惊喜",
                "delivery": "管理员会在群内公布盲盒结果。",
                "color": "#db2777",
                "enabled": True,
            },
        ]

    def _find_item(self, key: str) -> dict[str, Any] | None:
        normalized = key.strip().lower()
        if not normalized:
            return None
        for item in self._items():
            names = {str(item.get("id") or "").lower(), str(item.get("name") or "").lower()}
            if normalized in names:
                return item
        return None

    def _stock_left(self, item: dict[str, Any]) -> int:
        item_id = str(item.get("id") or "")
        configured = int(item.get("stock", -1) if item.get("stock", -1) is not None else -1)
        if configured < 0:
            return -1
        stock_map = self.state.setdefault("stock", {})
        if item_id not in stock_map:
            stock_map[item_id] = configured
        try:
            return int(stock_map.get(item_id, configured))
        except Exception:
            return configured

    def _balance(self, group_sid: str, user_id: str) -> int:
        return int(self.state.setdefault("balances", {}).setdefault(group_sid, {}).get(user_id, 0) or 0)

    def _add_points(self, group_sid: str, user_id: str, delta: int) -> int:
        balances = self.state.setdefault("balances", {}).setdefault(group_sid, {})
        current = int(balances.get(user_id, 0) or 0)
        next_value = max(0, current + int(delta))
        balances[user_id] = next_value
        return next_value

    def _next_streak(self, group_sid: str, user_id: str, today: str) -> int:
        streaks = self.state.setdefault("streaks", {}).setdefault(group_sid, {})
        old = streaks.get(user_id, {})
        old_day = str(old.get("last_day") or "")
        old_days = int(old.get("days") or 0)
        yesterday = self._days_ago(1)
        if old_day == yesterday:
            return old_days + 1
        if old_day == today:
            return old_days
        return 1

    def _set_streak(self, group_sid: str, user_id: str, days: int, today: str) -> None:
        self.state.setdefault("streaks", {}).setdefault(group_sid, {})[user_id] = {
            "days": int(days),
            "last_day": today,
        }

    def _streak_bonus(self, streak: int) -> int:
        every = max(0, self._cfg_int("streak_bonus_every_days", 7))
        bonus = max(0, self._cfg_int("streak_bonus_points", 20))
        if every <= 0 or bonus <= 0:
            return 0
        return bonus if streak > 0 and streak % every == 0 else 0

    def _days_ago(self, days: int) -> str:
        return datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")

    def _append_exchange_record(self, event: AstrMessageEvent, item: dict[str, Any], qty: int, cost: int) -> dict[str, Any]:
        records = self.state.setdefault("exchange_records", [])
        record = {
            "order_id": datetime.now().strftime("%Y%m%d%H%M%S") + str(random.randint(100, 999)),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform_id": event.get_platform_id(),
            "group_sid": self._group_sid(event),
            "group_id": self._group_id(event),
            "user_id": self._sender_id(event),
            "user_name": self._sender_name(event),
            "item_id": str(item.get("id") or ""),
            "item_name": str(item.get("name") or ""),
            "qty": int(qty),
            "cost": int(cost),
        }
        records.append(record)
        self._trim_records("exchange_records", self._cfg_int("max_exchange_records", 500))
        return record

    def _append_game_record(self, event: AstrMessageEvent, move: str, bot_move: str, bet: int, delta: int) -> None:
        self.state.setdefault("game_records", []).append(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "platform_id": event.get_platform_id(),
                "group_sid": self._group_sid(event),
                "group_id": self._group_id(event),
                "user_id": self._sender_id(event),
                "user_name": self._sender_name(event),
                "move": move,
                "bot_move": bot_move,
                "bet": int(bet),
                "delta": int(delta),
            }
        )
        self._trim_records("game_records", self._cfg_int("max_game_records", 500))

    def _trim_records(self, key: str, limit: int) -> None:
        limit = max(20, int(limit or 500))
        records = self.state.setdefault(key, [])
        if len(records) > limit:
            del records[:-limit]

    def _render_shop_poster(
        self,
        items: list[dict[str, Any]],
        stocks: dict[str, Any],
        balance: int,
        user_name: str,
    ) -> None:
        if Image is None or ImageDraw is None or ImageFont is None:
            raise RuntimeError("Pillow is not installed")

        width = max(900, self._cfg_int("poster_width", 1080))
        card_h = 170
        header_h = 240
        footer_h = 120
        gap = 26
        height = header_h + footer_h + len(items) * card_h + max(0, len(items) - 1) * gap + 72
        height = max(height, 980)

        img = Image.new("RGB", (width, height), "#172033")
        draw = ImageDraw.Draw(img)
        self._draw_vertical_gradient(draw, width, height, "#172033", "#25384e")
        self._draw_soft_circles(img)

        title_font = self._font(70, bold=True)
        sub_font = self._font(30)
        small_font = self._font(24)
        item_font = self._font(38, bold=True)
        price_font = self._font(36, bold=True)
        desc_font = self._font(25)
        icon_font = self._font(58, bold=True)

        margin = 58
        title = str(self._cfg_str("poster_title", "积分兑换商城"))
        subtitle = str(self._cfg_str("poster_subtitle", "签到积累，猜拳翻倍，把喜欢的奖励带回家"))
        draw.text((margin, 56), title, fill="#f8fafc", font=title_font)
        draw.text((margin + 4, 145), subtitle, fill="#cbd5e1", font=sub_font)
        self._rounded_rect(draw, (width - 330, 72, width - 58, 165), 28, fill="#f8fafc", outline=None)
        draw.text((width - 302, 91), "当前积分", fill="#475569", font=small_font)
        draw.text((width - 302, 121), str(balance), fill="#0f172a", font=price_font)

        y = header_h
        palette = ["#38bdf8", "#f97316", "#a78bfa", "#34d399", "#fb7185", "#facc15"]
        for idx, item in enumerate(items):
            x1, y1 = margin, y
            x2, y2 = width - margin, y + card_h
            base_color = self._safe_color(str(item.get("color") or ""), palette[idx % len(palette)])
            shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            sdraw = ImageDraw.Draw(shadow)
            self._rounded_rect(sdraw, (x1 + 8, y1 + 10, x2 + 8, y2 + 10), 24, fill=(0, 0, 0, 80), outline=None)
            shadow = shadow.filter(ImageFilter.GaussianBlur(14))
            img.paste(Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB"))
            draw = ImageDraw.Draw(img)

            self._rounded_rect(draw, (x1, y1, x2, y2), 24, fill="#f8fafc", outline="#ffffff")
            draw.rounded_rectangle((x1, y1, x1 + 16, y2), radius=8, fill=base_color)
            self._rounded_rect(draw, (x1 + 36, y1 + 34, x1 + 126, y1 + 124), 28, fill=base_color, outline=None)
            emoji = str(item.get("emoji") or "礼")
            self._draw_centered_text(draw, emoji, (x1 + 36, y1 + 34, x1 + 126, y1 + 124), icon_font, "#ffffff")

            name = str(item.get("name") or "")
            desc = str(item.get("description") or "暂无说明")
            item_id = str(item.get("id") or "")
            draw.text((x1 + 154, y1 + 32), name, fill="#0f172a", font=item_font)
            draw.text((x1 + 156, y1 + 85), self._ellipsize(desc, desc_font, width - 470), fill="#64748b", font=desc_font)
            draw.text((x1 + 156, y1 + 124), f"ID: {item_id}", fill="#94a3b8", font=small_font)

            price_box = (x2 - 245, y1 + 38, x2 - 44, y1 + 103)
            self._rounded_rect(draw, price_box, 22, fill="#0f172a", outline=None)
            self._draw_centered_text(draw, f"{int(item.get('price') or 0)} 积分", price_box, price_font, "#f8fafc")
            stock = stocks.get(item_id, item.get("stock", -1))
            stock_text = "库存：无限" if int(stock) < 0 else f"库存：{int(stock)}"
            draw.text((x2 - 226, y1 + 118), stock_text, fill="#64748b", font=small_font)
            y += card_h + gap

        footer_y = height - footer_h + 10
        draw.line((margin, footer_y - 16, width - margin, footer_y - 16), fill="#94a3b8", width=1)
        user_part = self._ellipsize(user_name, small_font, 260)
        draw.text((margin, footer_y), f"用户：{user_part}", fill="#e2e8f0", font=small_font)
        draw.text(
            (margin, footer_y + 38),
            "指令：/签到  /猜拳 石头 10  /兑换 商品ID 1  /兑换记录",
            fill="#cbd5e1",
            font=small_font,
        )
        draw.text(
            (width - 365, footer_y),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            fill="#cbd5e1",
            font=small_font,
        )

        self.poster_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(self.poster_path, "PNG")

    def _draw_vertical_gradient(self, draw: Any, width: int, height: int, top: str, bottom: str) -> None:
        top_rgb = self._hex_to_rgb(top)
        bottom_rgb = self._hex_to_rgb(bottom)
        for y in range(height):
            t = y / max(1, height - 1)
            rgb = tuple(int(top_rgb[i] * (1 - t) + bottom_rgb[i] * t) for i in range(3))
            draw.line((0, y, width, y), fill=rgb)

    def _draw_soft_circles(self, img: Any) -> None:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.ellipse((-160, -120, 360, 360), fill=(56, 189, 248, 52))
        draw.ellipse((760, 90, 1260, 590), fill=(251, 113, 133, 42))
        draw.ellipse((560, 760, 1160, 1360), fill=(52, 211, 153, 38))
        layer = layer.filter(ImageFilter.GaussianBlur(28))
        img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"))

    def _rounded_rect(self, draw: Any, box: tuple[int, int, int, int], radius: int, fill: Any, outline: Any = None) -> None:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2 if outline else 1)

    def _draw_centered_text(self, draw: Any, text: str, box: tuple[int, int, int, int], font: Any, fill: str) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = box[0] + (box[2] - box[0] - tw) / 2
        y = box[1] + (box[3] - box[1] - th) / 2 - 3
        draw.text((x, y), text, fill=fill, font=font)

    def _font(self, size: int, bold: bool = False) -> Any:
        candidates = [
            r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
            r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        for path in candidates:
            try:
                if path and Path(path).exists():
                    return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _ellipsize(self, text: str, font: Any, max_width: int) -> str:
        text = str(text or "")
        if not text:
            return ""
        dummy = Image.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy)
        if draw.textlength(text, font=font) <= max_width:
            return text
        suffix = "..."
        while text and draw.textlength(text + suffix, font=font) > max_width:
            text = text[:-1]
        return text + suffix

    def _safe_color(self, raw: str, fallback: str) -> str:
        raw = raw.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
            return raw
        return fallback

    def _hex_to_rgb(self, value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))

    def _shop_text(self, items: list[dict[str, Any]], stocks: dict[str, Any], balance: int) -> str:
        lines = [f"积分兑换商城\n当前积分：{balance}"]
        for item in items:
            item_id = str(item.get("id") or "")
            stock = stocks.get(item_id, item.get("stock", -1))
            stock_text = "无限" if int(stock) < 0 else str(int(stock))
            lines.append(
                f"{item.get('emoji', '🎁')} {item.get('name')} [{item_id}]\n"
                f"价格：{item.get('price')} | 库存：{stock_text}\n"
                f"{item.get('description', '')}"
            )
        lines.append("发送 /兑换 <商品ID或名称> [数量] 进行兑换。")
        return "\n\n".join(lines)

    def _image_component(self, path: Path):
        try:
            return Comp.Image.fromFileSystem(str(path))
        except Exception:
            try:
                return Comp.Image(file=str(path), path=str(path))
            except Exception:
                return Comp.Image(file=str(path))

    def _parse_rps_payload(self, payload: str) -> tuple[str | None, int | None]:
        parts = [part for part in re.split(r"\s+", payload.strip()) if part]
        move: str | None = None
        bet: int | None = None
        for part in parts:
            lower = part.lower()
            if lower in MOVE_ALIASES and move is None:
                move = MOVE_ALIASES[lower]
                continue
            if re.fullmatch(r"\d+", part) and bet is None:
                bet = int(part)
        return move, bet

    def _choose_rps_bot_move(self, player_move: str) -> str:
        win_rate = self._rps_win_rate()
        draw_rate = self._rps_draw_rate()
        roll = random.uniform(0, 100)
        if roll < win_rate:
            return WIN_MAP[player_move]
        if roll < win_rate + draw_rate:
            return player_move
        return LOSE_MAP[player_move]

    def _parse_exchange_payload(self, payload: str) -> tuple[str, int]:
        payload = payload.strip()
        if not payload:
            return "", 1
        parts = [part for part in re.split(r"\s+", payload) if part]
        if not parts:
            return "", 1
        qty = 1
        if len(parts) >= 2 and re.fullmatch(r"\d+", parts[-1]):
            qty = int(parts[-1])
            key = " ".join(parts[:-1])
        else:
            key = " ".join(parts)
        return key.strip(), qty

    def _parse_adjust_payload(self, event: AstrMessageEvent, payload: str) -> tuple[str, int | None]:
        at_user = self._extract_at_user(event)
        parts = [part for part in re.split(r"\s+", payload.strip()) if part]
        delta: int | None = None
        target = at_user or ""
        for part in parts:
            if re.fullmatch(r"[+-]?\d+", part):
                delta = int(part)
            elif not target:
                target = re.sub(r"\D", "", part) or part
        return target, delta

    def _extract_at_user(self, event: AstrMessageEvent) -> str:
        try:
            for comp in getattr(event, "message_obj", None).message:
                if getattr(comp, "type", "") == "At":
                    qq = str(getattr(comp, "qq", "") or getattr(comp, "target", "") or "").strip()
                    if qq:
                        return qq
        except Exception:
            return ""
        return ""

    def _help_text(self) -> str:
        return (
            "积分兑换系统指令：\n"
            "/签到 - 每日签到领取积分\n"
            "/积分 - 查看当前积分\n"
            "/积分排行 - 查看本群排行榜\n"
            f"/猜拳 <石头|剪刀|布> <积分> - 下注猜拳，范围 {self._min_bet()}~{self._max_bet()}，当前胜率 {self._rps_win_rate()}%\n"
            "/商店 - 查看精美商品图\n"
            "/兑换 <商品ID或名称> [数量] - 消耗积分兑换\n"
            "/兑换记录 - 查看最近订单"
        )

    def _remember_user(self, event: AstrMessageEvent) -> None:
        group_sid = self._group_sid(event)
        user_id = self._sender_id(event)
        self.state.setdefault("profiles", {}).setdefault(group_sid, {})[user_id] = {
            "name": self._sender_name(event),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _reply_and_stop(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(str(text or "")))
        event.stop_event()

    def _command_payload(self, event: AstrMessageEvent, names: tuple[str, ...]) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        for name in sorted(names, key=len, reverse=True):
            for prefix in ("/", "／", ""):
                mark = prefix + name
                if text == mark:
                    return ""
                if text.startswith(mark):
                    return text[len(mark) :].strip()
        return text

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        try:
            if event.get_group_id():
                return True
        except Exception:
            pass

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if f":{GROUP_MESSAGE_TYPE}:" in umo or ":group:" in umo.lower():
            return True

        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return False

        for attr in ("type", "message_type", "event_type"):
            value = str(getattr(message_obj, attr, "") or "")
            if value == GROUP_MESSAGE_TYPE or value.lower() in {"group", "groupmessage", "group_message"}:
                return True

        return bool(getattr(message_obj, "group_id", "") or getattr(message_obj, "group", ""))

    def _group_sid(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            return umo
        platform = event.get_platform_id()
        group_id = self._group_id(event)
        return f"{platform}:GroupMessage:{group_id}"

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = event.get_group_id()
            if group_id:
                return str(group_id)
        except Exception:
            pass
        try:
            return str(getattr(event.message_obj, "group_id", "") or "")
        except Exception:
            return ""

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            pass
        try:
            return str(getattr(event.message_obj, "sender", {}).get("user_id") or "")
        except Exception:
            return ""

    def _sender_name(self, event: AstrMessageEvent) -> str:
        try:
            name = event.get_sender_name()
            if name:
                return str(name)
        except Exception:
            pass
        try:
            sender = getattr(event.message_obj, "sender", {}) or {}
            return str(sender.get("nickname") or sender.get("card") or self._sender_id(event))
        except Exception:
            return self._sender_id(event)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _resolve_path(self, raw: str) -> Path:
        path = Path(str(raw or "").strip() or "data/state.json")
        if path.is_absolute():
            return path
        return self.plugin_dir / path

    def _enabled(self) -> bool:
        return self._cfg_bool("enabled", True)

    def _min_bet(self) -> int:
        return max(1, self._cfg_int("min_bet", 5))

    def _max_bet(self) -> int:
        return max(self._min_bet(), self._cfg_int("max_bet", 100))

    def _rps_win_rate(self) -> int:
        return self._percent(self._cfg_int("rps_win_rate", 33))

    def _rps_draw_rate(self) -> int:
        return min(self._percent(self._cfg_int("rps_draw_rate", 33)), max(0, 100 - self._rps_win_rate()))

    def _percent(self, value: int) -> int:
        return max(0, min(100, int(value)))

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self.config.get(key, default) if self.config.get(key, None) is not None else default)

    def _cfg_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:
            return int(default)

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        return self._to_bool(self.config.get(key, default), default)

    def _to_bool(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "开启", "启用"}
