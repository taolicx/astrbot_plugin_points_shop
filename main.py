from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from quart import jsonify, request

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover - runtime dependency guard
    Image = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None


PLUGIN_NAME = "astrbot_plugin_points_shop"
PLUGIN_VERSION = "0.1.8"
GROUP_MESSAGE_TYPE = "GroupMessage"
FRIEND_MESSAGE_TYPE = "FriendMessage"

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
        self._register_web_apis()
        logger.info("[PointsShop] initialized")

    async def terminate(self):
        await asyncio.to_thread(self._save_state)
        logger.info("[PointsShop] terminated")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=90)
    async def group_text_entry(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            return
        if self._looks_like_explicit_command(event):
            return

        handler = self._match_group_text_handler(event)
        if handler is None:
            return

        await handler(event)
        event.stop_event()

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
            signins = self.state.setdefault("signins", {})
            last_day = str(signins.get(user_id) or "")
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

            signins[user_id] = today
            self._set_streak(group_sid, user_id, streak, today)
            balance = self._add_points(group_sid, user_id, total)
            self._save_state()

        bonus_text = f"\n连续签到奖励：+{bonus}" if bonus else ""
        await self._reply_and_stop(event, f"签到成功，获得 {total} 积分！{bonus_text}\n连续签到：{streak} 天\n当前积分：{balance}")
    @filter.command("积分", alias={"我的积分", "余额"}, priority=100)
    async def show_points(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "积分查询需要在群聊里进行。")
            return

        async with self._lock:
            self._remember_user(event)
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            balance = self._balance(group_sid, user_id)
            streak = self.state.setdefault("streaks", {}).get(user_id, {}).get("days", 0)

        await self._reply_and_stop(event, f"{self._sender_name(event)}\n当前积分：{balance}\n连续签到：{streak} 天")
    @filter.command("积分排行", alias={"排行榜", "积分榜"}, priority=100)
    async def leaderboard(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_group_event(event):
            await self._reply_and_stop(event, "积分排行需要在群聊里查看。")
            return

        async with self._lock:
            balances = dict(self.state.setdefault("balances", {}))
            profiles = dict(self.state.setdefault("profiles", {}))

        if not balances:
            await self._reply_and_stop(event, "还没有积分记录，先发 签到 开始积累吧。")
            return

        limit = max(3, min(20, self._cfg_int("leaderboard_limit", 10)))
        rows = sorted(balances.items(), key=lambda item: int(item[1] or 0), reverse=True)[:limit]
        lines = ["全局积分排行："]
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
                f"用法：猜拳 <石头|剪刀|布> <积分>\n下注范围：{self._min_bet()}~{self._max_bet()} 积分",
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
                Comp.Plain(text=f"\n当前积分：{balance}\n发送 兑换 <商品ID或名称> [数量] 进行兑换。"),
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
            await self._reply_and_stop(event, "用法：兑换 <商品ID或名称> [数量]\n发送 商店 查看可兑换商品。")
            return

        qty = max(1, qty)
        reward_entries: list[dict[str, Any]] = []
        async with self._lock:
            group_sid = self._group_sid(event)
            user_id = self._sender_id(event)
            self._remember_user(event)
            item = self._find_item(item_key)
            if not item:
                await self._reply_and_stop(event, "没有找到这个商品。发送 商店 查看商品 ID 和名称。")
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

            if self._reward_mode(item) == "pool":
                pool = self._reward_pool(item_id)
                if len(pool) < qty:
                    await self._reply_and_stop(event, f"该商品的兑换码仓库不足，当前可发送：{len(pool)}")
                    return

            self._add_points(group_sid, user_id, -cost)
            if stock >= 0:
                self.state.setdefault("stock", {})[item_id] = stock - qty
            if self._reward_mode(item) == "pool":
                pool = self._reward_pool(item_id)
                reward_entries = [pool.pop(0) for _ in range(qty)]
            record = self._append_exchange_record(event, item, qty, cost)
            new_balance = self._balance(group_sid, user_id)
            self._save_state()

        private_ok = True
        if reward_entries:
            private_ok = await self._send_reward_private(event, item, record, reward_entries)

        if reward_entries:
            private_text = "奖励已私发，请查收私聊。" if private_ok else "奖励发送失败，请联系管理员补发。"
            await self._reply_and_stop(
                event,
                f"兑换成功！\n"
                f"商品：{item.get('name')} x{qty}\n"
                f"消耗：{cost} 积分\n"
                f"订单号：{record['order_id']}\n"
                f"{private_text}\n"
                f"剩余积分：{new_balance}",
            )
            return

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

        user_id = self._sender_id(event)
        platform_id = event.get_platform_id()
        async with self._lock:
            records = [
                item
                for item in self.state.setdefault("exchange_records", [])
                if item.get("platform_id") == platform_id and item.get("user_id") == user_id
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
            await self._reply_and_stop(event, "用法：积分管理 <用户ID或@用户> <+/-积分>\n例：积分管理 123456 +50")
            return

        async with self._lock:
            new_balance = self._add_points(self._group_sid(event), target_id, delta)
            self._save_state()

        await self._reply_and_stop(event, f"已调整积分：{target_id} {delta:+d}\n当前积分：{new_balance}")

    @filter.command("奖励入库", alias={"入库奖励", "兑换入库"}, priority=100)
    async def reward_pool_add(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_admin(event):
            await self._reply_and_stop(event, "只有管理员可以添加兑换码。")
            return

        payload = self._command_payload(event, ("奖励入库", "入库奖励", "兑换入库"))
        item_key, code, note = self._parse_reward_pool_add_payload(payload)
        if not item_key or not code:
            await self._reply_and_stop(
                event,
                "用法：奖励入库 <商品ID> <兑换码> [| 备注]\n"
                "例：奖励入库 mystery ABCD-EFGH-IJKL\n"
                "例：奖励入库 mystery ABCD-EFGH-IJKL | 第一批兑换码",
            )
            return

        async with self._lock:
            item = self._find_item(item_key)
            if not item:
                await self._reply_and_stop(event, "没有找到这个商品。")
                return
            if self._reward_mode(item) != "pool":
                await self._reply_and_stop(event, "这个商品当前不是仓库发货模式，请先把 reward_mode 设置为 pool。")
                return

            item_id = str(item.get("id") or "")
            entry = self._make_reward_entry(code, note)
            pool = self._reward_pool(item_id)
            pool.append(entry)
            count = len(pool)
            self._save_state()

        await self._reply_and_stop(event, f"已入库：{item.get('name')}\n兑换码：{code}\n当前库存：{count}")

    @filter.command("奖励仓库", alias={"兑换仓库", "奖励列表"}, priority=100)
    async def reward_pool_list(self, event: AstrMessageEvent):
        if not self._enabled():
            return
        if not self._is_admin(event):
            await self._reply_and_stop(event, "只有管理员可以查看兑换码仓库。")
            return

        payload = self._command_payload(event, ("奖励仓库", "兑换仓库", "奖励列表"))
        item_key = str(payload or "").strip()
        async with self._lock:
            if item_key:
                item = self._find_item(item_key)
                if not item:
                    await self._reply_and_stop(event, "没有找到这个商品。")
                    return
                item_id = str(item.get("id") or "")
                pool = list(self._reward_pool(item_id))
                lines = [f"{item.get('name')} 兑换码仓库：共 {len(pool)} 条"]
                for index, entry in enumerate(pool[:10], start=1):
                    preview = self._format_reward_entry(entry).replace("\n", " | ")
                    if len(preview) > 60:
                        preview = preview[:60] + "..."
                    lines.append(f"{index}. {preview}")
                if len(pool) > 10:
                    lines.append(f"... 其余 {len(pool) - 10} 条未展开")
                await self._reply_and_stop(event, "\n".join(lines))
                return

            lines = ["当前各商品兑换码仓库："]
            for item in self._items():
                item_id = str(item.get("id") or "")
                mode_label = "兑换码私发" if self._reward_mode(item) == "pool" else "手动核销"
                lines.append(f"{item.get('name')} [{item_id}] - {len(self._reward_pool(item_id))} 条 - 模式：{mode_label}")
            await self._reply_and_stop(event, "\n".join(lines))

    async def api_admin_items(self):
        async with self._lock:
            items = [self._serialize_admin_item(item) for item in self._items()]
        return self._api_ok({"items": items})

    async def api_admin_codes(self):
        item_key = str(request.args.get("item_id", "") or "").strip()
        keyword = str(request.args.get("keyword", "") or "").strip().lower()
        if not item_key:
            return self._api_error("缺少 item_id 参数。")

        async with self._lock:
            item = self._find_item(item_key)
            if not item:
                return self._api_error("没有找到这个商品。", 404)
            if self._reward_mode(item) != "pool":
                return self._api_error("这个商品当前不是兑换码仓库模式。")

            pool = list(self._reward_pool(str(item.get("id") or "")))
            if keyword:
                pool = [
                    entry
                    for entry in pool
                    if keyword in str(entry.get("code") or "").lower() or keyword in str(entry.get("note") or "").lower()
                ]

            codes = [self._serialize_reward_entry(entry) for entry in pool]
            data = {
                "item": self._serialize_admin_item(item),
                "codes": codes,
                "total": len(codes),
            }
        return self._api_ok(data)

    async def api_admin_codes_bulk_add(self):
        body = await self._request_json()
        item_key = str(body.get("item_id") or body.get("item_key") or "").strip()
        codes_text = str(body.get("codes_text") or "").replace("\r\n", "\n")
        note = str(body.get("note") or "").strip()
        if not item_key:
            return self._api_error("请选择要入库的商品。")
        if not codes_text.strip():
            return self._api_error("请输入兑换码，每行一个。")

        raw_codes = [line.strip() for line in codes_text.split("\n")]
        seen: set[str] = set()
        codes: list[str] = []
        duplicate_in_payload = 0
        for code in raw_codes:
            if not code:
                continue
            if code in seen:
                duplicate_in_payload += 1
                continue
            seen.add(code)
            codes.append(code)

        if not codes:
            return self._api_error("没有可用的兑换码内容。")

        async with self._lock:
            item = self._find_item(item_key)
            if not item:
                return self._api_error("没有找到这个商品。", 404)
            if self._reward_mode(item) != "pool":
                return self._api_error("这个商品当前不是兑换码仓库模式。")

            item_id = str(item.get("id") or "")
            pool = self._reward_pool(item_id)
            existing_codes = {str(entry.get("code") or "") for entry in pool}
            added_entries: list[dict[str, Any]] = []
            skipped_existing = 0

            for code in codes:
                if code in existing_codes:
                    skipped_existing += 1
                    continue
                entry = self._make_reward_entry(code, note)
                pool.append(entry)
                existing_codes.add(code)
                added_entries.append(entry)

            self._save_state()
            data = {
                "item": self._serialize_admin_item(item),
                "added_count": len(added_entries),
                "skipped_existing": skipped_existing,
                "skipped_duplicate_input": duplicate_in_payload,
                "total_count": len(pool),
                "codes": [self._serialize_reward_entry(entry) for entry in added_entries],
            }
        return self._api_ok(data, f"已添加 {len(added_entries)} 个兑换码。")

    async def api_admin_codes_delete(self):
        body = await self._request_json()
        item_key = str(body.get("item_id") or body.get("item_key") or "").strip()
        reward_id = str(body.get("reward_id") or "").strip()
        if not item_key or not reward_id:
            return self._api_error("缺少 item_id 或 reward_id。")

        async with self._lock:
            item = self._find_item(item_key)
            if not item:
                return self._api_error("没有找到这个商品。", 404)
            if self._reward_mode(item) != "pool":
                return self._api_error("这个商品当前不是兑换码仓库模式。")

            item_id = str(item.get("id") or "")
            pool = self._reward_pool(item_id)
            next_pool = [entry for entry in pool if str(entry.get("id") or "") != reward_id]
            if len(next_pool) == len(pool):
                return self._api_error("没有找到要删除的兑换码。", 404)

            self.state.setdefault("reward_pool", {})[item_id] = next_pool
            self._save_state()
            data = {
                "item": self._serialize_admin_item(item),
                "total_count": len(next_pool),
            }
        return self._api_ok(data, "兑换码已删除。")

    async def api_admin_codes_clear(self):
        body = await self._request_json()
        item_key = str(body.get("item_id") or body.get("item_key") or "").strip()
        if not item_key:
            return self._api_error("缺少 item_id。")

        async with self._lock:
            item = self._find_item(item_key)
            if not item:
                return self._api_error("没有找到这个商品。", 404)
            if self._reward_mode(item) != "pool":
                return self._api_error("这个商品当前不是兑换码仓库模式。")

            item_id = str(item.get("id") or "")
            count = len(self._reward_pool(item_id))
            self.state.setdefault("reward_pool", {})[item_id] = []
            self._save_state()
            data = {
                "item": self._serialize_admin_item(item),
                "cleared_count": count,
                "total_count": 0,
            }
        return self._api_ok(data, f"已清空 {count} 个兑换码。")

    def _register_web_apis(self) -> None:
        routes = [
            ("items", self.api_admin_items, ["GET"], "积分商城兑换码管理：商品列表"),
            ("codes", self.api_admin_codes, ["GET"], "积分商城兑换码管理：查询兑换码"),
            ("codes/bulk-add", self.api_admin_codes_bulk_add, ["POST"], "积分商城兑换码管理：批量添加兑换码"),
            ("codes/delete", self.api_admin_codes_delete, ["POST"], "积分商城兑换码管理：删除兑换码"),
            ("codes/clear", self.api_admin_codes_clear, ["POST"], "积分商城兑换码管理：清空兑换码"),
        ]
        for suffix, handler, methods, desc in routes:
            normalized = str(suffix).strip().lstrip("/")
            self.context.register_web_api(f"/points-shop/admin/{normalized}", handler, methods, desc)
            self.context.register_web_api(f"/{PLUGIN_NAME}/points-shop/admin/{normalized}", handler, methods, desc)

    async def _request_json(self) -> dict[str, Any]:
        try:
            body = await request.get_json(silent=True)
        except Exception:
            body = None
        return body if isinstance(body, dict) else {}

    def _api_ok(self, data: dict[str, Any] | list[Any] | None = None, message: str | None = None):
        return jsonify(
            {
                "status": "ok",
                "message": message,
                "data": {} if data is None else data,
            }
        )

    def _api_error(self, message: str, status_code: int = 400):
        response = jsonify(
            {
                "status": "error",
                "message": message,
                "data": {},
            }
        )
        response.status_code = status_code
        return response

    def _serialize_admin_item(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item.get("id") or "")
        mode = self._reward_mode(item)
        return {
            "id": item_id,
            "name": str(item.get("name") or ""),
            "price": int(item.get("price") or 0),
            "description": str(item.get("description") or ""),
            "delivery": str(item.get("delivery") or ""),
            "emoji": str(item.get("emoji") or ""),
            "reward_mode": mode,
            "reward_mode_label": "兑换码仓库私发" if mode == "pool" else "手动核销",
            "pool_size": len(self._reward_pool(item_id)),
            "stock_left": self._stock_left(item),
            "is_pool": mode == "pool",
        }

    def _serialize_reward_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(entry.get("id") or ""),
            "code": str(entry.get("code") or ""),
            "note": str(entry.get("note") or ""),
            "created_at": str(entry.get("created_at") or ""),
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "balances": {},
            "profiles": {},
            "signins": {},
            "streaks": {},
            "stock": {},
            "reward_pool": {},
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
            self._migrate_legacy_state()
        except Exception as exc:
            logger.warning(f"[PointsShop] load state failed: {exc}")

    def _migrate_legacy_state(self) -> None:
        self.state["balances"] = self._merge_balance_state(self.state.get("balances"))
        self.state["signins"] = self._merge_signin_state(self.state.get("signins"))
        self.state["streaks"] = self._merge_streak_state(self.state.get("streaks"))
        self.state["profiles"] = self._merge_profile_state(self.state.get("profiles"))
        self.state["reward_pool"] = self._normalize_reward_pool_state(self.state.get("reward_pool"))

    def _merge_balance_state(self, raw: Any) -> dict[str, int]:
        merged: dict[str, int] = {}
        if not isinstance(raw, dict):
            return merged
        for key, value in raw.items():
            if isinstance(value, dict):
                for user_id, points in value.items():
                    uid = str(user_id)
                    merged[uid] = max(0, merged.get(uid, 0) + int(points or 0))
            else:
                uid = str(key)
                merged[uid] = max(0, int(value or 0))
        return merged

    def _merge_signin_state(self, raw: Any) -> dict[str, str]:
        merged: dict[str, str] = {}
        if not isinstance(raw, dict):
            return merged
        for key, value in raw.items():
            if isinstance(value, dict):
                for user_id, last_day in value.items():
                    uid = str(user_id)
                    day = str(last_day or "")
                    if day and day > merged.get(uid, ""):
                        merged[uid] = day
            else:
                uid = str(key)
                day = str(value or "")
                if day and day > merged.get(uid, ""):
                    merged[uid] = day
        return merged

    def _merge_streak_state(self, raw: Any) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return merged
        for key, value in raw.items():
            if isinstance(value, dict) and ("days" in value or "last_day" in value):
                self._merge_one_streak_entry(merged, str(key), value)
            elif isinstance(value, dict):
                for user_id, entry in value.items():
                    if isinstance(entry, dict):
                        self._merge_one_streak_entry(merged, str(user_id), entry)
        return merged

    def _merge_one_streak_entry(self, merged: dict[str, dict[str, Any]], user_id: str, entry: dict[str, Any]) -> None:
        candidate = {"days": max(0, int(entry.get("days") or 0)), "last_day": str(entry.get("last_day") or "")}
        current = merged.get(user_id)
        if current is None:
            merged[user_id] = candidate
            return
        if candidate["last_day"] > str(current.get("last_day") or ""):
            merged[user_id] = candidate
            return
        if candidate["last_day"] == str(current.get("last_day") or "") and candidate["days"] > int(current.get("days") or 0):
            merged[user_id] = candidate

    def _merge_profile_state(self, raw: Any) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return merged
        for key, value in raw.items():
            if isinstance(value, dict) and ("name" in value or "updated_at" in value):
                self._merge_one_profile_entry(merged, str(key), value)
            elif isinstance(value, dict):
                for user_id, entry in value.items():
                    if isinstance(entry, dict):
                        self._merge_one_profile_entry(merged, str(user_id), entry)
        return merged

    def _merge_one_profile_entry(self, merged: dict[str, dict[str, Any]], user_id: str, entry: dict[str, Any]) -> None:
        candidate = {"name": str(entry.get("name") or user_id), "updated_at": str(entry.get("updated_at") or "")}
        current = merged.get(user_id)
        if current is None or candidate["updated_at"] >= str(current.get("updated_at") or ""):
            merged[user_id] = candidate
    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[PointsShop] save state failed: {exc}")

    def _sync_stock_from_config(self) -> None:
        stock_map = self.state.setdefault("stock", {})
        reward_pool = self._normalize_reward_pool_state(self.state.get("reward_pool"))
        self.state["reward_pool"] = reward_pool
        for item in self._items():
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            configured = int(item.get("stock", -1) if item.get("stock", -1) is not None else -1)
            if item_id not in stock_map:
                stock_map[item_id] = configured
            elif configured < 0:
                stock_map[item_id] = -1
            reward_pool.setdefault(item_id, [])
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
            reward_mode = str(item.get("reward_mode") or "manual").strip().lower()
            if reward_mode not in {"manual", "pool"}:
                reward_mode = "manual"
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
                    "reward_mode": reward_mode,
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
                "reward_mode": "manual",
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
                "reward_mode": "manual",
                "enabled": True,
            },
            {
                "id": "mystery",
                "name": "神秘盲盒",
                "price": 300,
                "stock": 3,
                "emoji": "🎁",
                "description": "随机小奖励，开盒有惊喜",
                "delivery": "奖励会通过私聊自动发放。",
                "color": "#db2777",
                "reward_mode": "pool",
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
        return int(self.state.setdefault("balances", {}).setdefault(user_id, 0) or 0)

    def _add_points(self, group_sid: str, user_id: str, delta: int) -> int:
        balances = self.state.setdefault("balances", {})
        current = int(balances.get(user_id, 0) or 0)
        next_value = max(0, current + int(delta))
        balances[user_id] = next_value
        return next_value

    def _next_streak(self, group_sid: str, user_id: str, today: str) -> int:
        streaks = self.state.setdefault("streaks", {})
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
        self.state.setdefault("streaks", {})[user_id] = {"days": int(days), "last_day": today}
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

    def _reward_mode(self, item: dict[str, Any]) -> str:
        mode = str(item.get("reward_mode") or "manual").strip().lower()
        return mode if mode in {"manual", "pool"} else "manual"

    def _reward_pool(self, item_id: str) -> list[dict[str, Any]]:
        reward_pool = self.state.setdefault("reward_pool", {})
        key = str(item_id)
        pool = reward_pool.get(key, [])
        if not isinstance(pool, list):
            pool = []
        normalized_pool: list[dict[str, Any]] = []
        for raw_entry in pool:
            entry = self._normalize_reward_entry(raw_entry)
            if entry is not None:
                normalized_pool.append(entry)
        reward_pool[key] = normalized_pool
        return normalized_pool

    def _new_reward_entry_id(self) -> str:
        return uuid4().hex

    def _make_reward_entry(self, code: str, note: str = "", created_at: str = "", reward_id: str = "") -> dict[str, Any]:
        normalized_code = str(code or "").strip()
        normalized_note = str(note or "").strip()
        return {
            "id": str(reward_id or self._new_reward_entry_id()).strip(),
            "code": normalized_code,
            "note": normalized_note,
            "created_at": str(created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")).strip(),
        }

    def _normalize_reward_entry(self, raw_entry: Any) -> dict[str, Any] | None:
        if isinstance(raw_entry, str):
            code = raw_entry.strip()
            return self._make_reward_entry(code) if code else None

        if not isinstance(raw_entry, dict):
            return None

        code = str(
            raw_entry.get("code")
            or raw_entry.get("payload")
            or raw_entry.get("content")
            or ""
        ).strip()
        if not code:
            return None

        return self._make_reward_entry(
            code=code,
            note=str(raw_entry.get("note") or "").strip(),
            created_at=str(raw_entry.get("created_at") or "").strip(),
            reward_id=str(raw_entry.get("id") or "").strip(),
        )

    def _normalize_reward_pool_state(self, raw_pool: Any) -> dict[str, list[dict[str, Any]]]:
        normalized: dict[str, list[dict[str, Any]]] = {}
        if not isinstance(raw_pool, dict):
            return normalized

        for raw_item_id, raw_entries in raw_pool.items():
            item_id = str(raw_item_id or "").strip()
            if not item_id:
                continue
            entries: list[dict[str, Any]] = []
            if isinstance(raw_entries, list):
                for raw_entry in raw_entries:
                    entry = self._normalize_reward_entry(raw_entry)
                    if entry is not None:
                        entries.append(entry)
            normalized[item_id] = entries
        return normalized

    def _format_reward_entry(self, entry: dict[str, Any]) -> str:
        code = str(entry.get("code") or "").strip()
        note = str(entry.get("note") or "").strip()
        text = f"兑换码：{code}"
        if note:
            text = f"{text}\n备注：{note}"
        return text

    async def _send_reward_private(self, event: AstrMessageEvent, item: dict[str, Any], record: dict[str, Any], reward_entries: list[dict[str, Any]]) -> bool:
        private_sid = self._private_sid(event.get_platform_id(), self._sender_id(event))
        lines = ["你兑换到的奖励：", f"商品：{item.get('name')}", f"订单号：{record.get('order_id')}", ""]
        for index, entry in enumerate(reward_entries, start=1):
            code = str(entry.get("code") or "").strip()
            note = str(entry.get("note") or "").strip()
            lines.append(f"{index}. 兑换码：{code}")
            if note:
                lines.append(f"   备注：{note}")
            lines.append("")
        if lines:
            try:
                sent = await self.context.send_message(private_sid, MessageChain([Comp.Plain(text="\n".join(part for part in lines if part))]))
                if not sent:
                    logger.warning(f"[PointsShop] private reward send returned false: {private_sid}")
                    return False
                return True
            except Exception as exc:
                logger.warning(f"[PointsShop] private reward send failed: {private_sid}, {exc}")
                return False
        return True
    def _parse_reward_pool_add_payload(self, payload: str) -> tuple[str, str, str]:
        payload = str(payload or "").strip()
        if not payload:
            return "", "", ""
        note = ""
        if "|" in payload:
            payload, note = payload.split("|", 1)
            payload = payload.strip()
            note = note.strip()
        parts = [part for part in re.split(r"\s+", payload) if part]
        if len(parts) < 2:
            return "", "", note
        item_key = parts[0].strip()
        code = " ".join(parts[1:]).strip()
        return item_key, code, note
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
            "签到 - 每日签到领取积分（所有群聊共享积分）\n"
            "积分 - 查看当前积分余额\n"
            "积分排行 - 查看全局积分排行榜\n"
            f"猜拳 <石头|剪刀|布> <积分> - 下注猜拳，范围 {self._min_bet()}~{self._max_bet()}，当前胜率 {self._rps_win_rate()}%\n"
            "商店 - 查看精美商品图\n"
            "兑换 <商品ID或名称> [数量] - 消耗积分兑换\n"
            "兑换记录 - 查看最近订单\n"
            "奖励入库 <商品ID> <兑换码> [| 备注] - 管理员手动补充兑换码\n"
            "奖励仓库 [商品ID] - 管理员查看兑换码仓库\n"
            "推荐在插件页面 code_manager 中批量维护兑换码。\n"
            "仓库发货商品兑换后会在群里提示奖励已私发，兑换码通过私聊发送。\n"
            "以上指令可直接发纯文字，也兼容 / 前缀。"
        )

    def _remember_user(self, event: AstrMessageEvent) -> None:
        user_id = self._sender_id(event)
        self.state.setdefault("profiles", {})[user_id] = {
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

    def _looks_like_explicit_command(self, event: AstrMessageEvent) -> bool:
        text = str(getattr(event, "message_str", "") or "").strip()
        return text.startswith("/") or text.startswith("／")

    def _match_group_text_handler(self, event: AstrMessageEvent):
        text = self._normalized_text(event)
        if not text:
            return None

        exact_handlers = {
            "签到": self.sign_in,
            "打卡": self.sign_in,
            "每日签到": self.sign_in,
            "积分": self.show_points,
            "我的积分": self.show_points,
            "余额": self.show_points,
            "积分排行": self.leaderboard,
            "排行榜": self.leaderboard,
            "积分榜": self.leaderboard,
            "商店": self.shop,
            "兑换商城": self.shop,
            "积分商城": self.shop,
            "商品列表": self.shop,
            "兑换列表": self.shop,
            "兑换记录": self.exchange_records,
            "我的兑换": self.exchange_records,
            "订单": self.exchange_records,
            "积分帮助": self.help_text,
            "兑换帮助": self.help_text,
            "商店帮助": self.help_text,
            "奖励仓库": self.reward_pool_list,
            "兑换仓库": self.reward_pool_list,
            "奖励列表": self.reward_pool_list,
        }
        if text in exact_handlers:
            return exact_handlers[text]

        prefix_handlers = (
            ("猜拳", self.rps),
            ("剪刀石头布", self.rps),
            ("石头剪刀布", self.rps),
            ("划拳", self.rps),
            ("兑换", self.exchange),
            ("购买", self.exchange),
            ("积分兑换", self.exchange),
            ("积分管理", self.manage_points),
            ("积分调整", self.manage_points),
            ("奖励入库", self.reward_pool_add),
            ("入库奖励", self.reward_pool_add),
            ("兑换入库", self.reward_pool_add),
            ("奖励仓库", self.reward_pool_list),
            ("兑换仓库", self.reward_pool_list),
            ("奖励列表", self.reward_pool_list),
        )
        for prefix, handler in prefix_handlers:
            if text == prefix or text.startswith(prefix + " "):
                return handler
        return None
    def _normalized_text(self, event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = text.replace("\u3000", " ")
        text = re.sub(r"\s+", " ", text)
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

    def _private_sid(self, platform_id: str, user_id: str) -> str:
        return f"{platform_id}:{FRIEND_MESSAGE_TYPE}:{user_id}"
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
