"""神祇资料层 —— 本地神祇卡 + 缺卡时的搜索要求，独立于任何占卜插件。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

MAIBOT_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path.cwd()).expanduser().resolve()
RESOURCE_ROOT = MAIBOT_ROOT / "data" / "daimon-resources"


def _configured_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else MAIBOT_ROOT / path


PACK_DIR = RESOURCE_ROOT / "deity-cards"

_EXTRA_ALIASES = {
    "提丰": "typhon",
    "堤福俄斯": "typhon",
    "狄俄尼索斯": "dionysus",
    "狄奥尼索斯": "dionysus",
    "阿佛洛狄忒": "aphrodite",
    "阿芙罗狄蒂": "aphrodite",
    "赫卡提": "hecate",
    "厄里倪厄斯": "erinyes",
    "复仇女神": "erinyes",
    "欧墨尼得斯": "erinyes",
    "赫尔墨斯·三重伟大": "hermes",
}


def _clean_alias(text: str) -> str:
    return re.sub(r"[（(].*?[)）]", "", str(text)).strip()


def _extract_text(payload: Any) -> str:
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.strip():
                chunks.append(node)
        elif isinstance(node, dict):
            for key in ("text", "plain_text", "processed_plain_text", "content"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    chunks.append(value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return " ".join(chunks)


class DeityLorePlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._cards: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        self._known: set[str] = set()
        self._load_pack()

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "神祇资料层已加载：本地卡 %s 位，可识别神名 %s 个",
            len(self._cards),
            len(self._known),
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, config: Any = None) -> None:
        self._load_pack()
        return None

    def _load_pack(self) -> None:
        cards_path = PACK_DIR / "cards.json"
        catalog_path = PACK_DIR / "catalog.json"
        if not cards_path.exists():
            return
        try:
            cards = json.loads(cards_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else []
        except Exception:
            return
        for slug, card in cards.items():
            self._cards[slug] = card
            for key in ("zh", "en", "grc"):
                value = card.get(key)
                if value:
                    self._aliases[str(value)] = slug
            for alt in card.get("alt") or []:
                name = _clean_alias(str(alt))
                if name:
                    self._aliases[name] = slug
        for item in catalog:
            for key in ("zh", "en"):
                value = item.get(key)
                if value:
                    self._known.add(str(value))
        for alias, slug in _EXTRA_ALIASES.items():
            self._aliases[alias] = slug
            self._known.add(alias)

    def _find(self, text: str) -> tuple[dict | None, str]:
        if not text:
            return None, ""
        for alias, slug in sorted(self._aliases.items(), key=lambda kv: len(kv[0]), reverse=True):
            if alias and alias in text:
                return self._cards.get(slug), alias
        for name in sorted(self._known, key=len, reverse=True):
            if name and name in text:
                return None, name
        return None, ""

    @staticmethod
    def _brief_text(card: dict) -> str:
        """给回复用的极简版：只留一两句，避免变成百科词条。"""
        summary = (card.get("summary") or "").strip()
        first = re.split(r"[。；;]", summary)[0] if summary else ""
        bits = [first] if first else []
        symbols = card.get("symbols") or []
        if symbols:
            bits.append("象征：" + "、".join(symbols[:2]))
        myths = card.get("myths") or []
        if myths and len(bits) < 2:
            bits.append("相关：" + str(myths[0].get("title", "")))
        return "；".join(b for b in bits if b)

    @staticmethod
    def _card_text(card: dict) -> str:
        parts: list[str] = []
        if card.get("summary"):
            parts.append(f"要义：{card['summary']}")
        if card.get("domain"):
            parts.append("职能：" + "、".join(card["domain"]))
        if card.get("symbols"):
            parts.append("象征：" + "、".join(card["symbols"]))
        if card.get("epithets"):
            parts.append("圣号：" + "、".join(card["epithets"]))
        myths = card.get("myths") or []
        if myths:
            parts.append(
                "神话点：" + "；".join(f"{m.get('title', '')}——{m.get('summary', '')}" for m in myths[:2])
            )
        if card.get("sources"):
            parts.append("出处：" + "、".join(card["sources"]))
        return f"【{card.get('zh', '')}（{card.get('en', '')} / {card.get('grc', '')}）· 本地神祇库】\n" + "\n".join(parts)

    @Tool(
        "deity_lore",
        brief_description="查本地神祇资料库：问某个神的职能、象征、神话与出处",
        detailed_description=(
            "当消息里出现某个神/神祇（希腊神名，如雅典娜、赫尔墨斯、复仇三女神），"
            "而你要回答关于祂的看法、印象、象征或神话时，先调用本工具。\n"
            "返回本地神祇卡的依据，可以直接用来组织回答；不要背书、不要报出处清单。\n"
            "如果返回「本地神祇库没有」，你必须先调用 web_search 查证，再回答；"
            "查不到就直说不知道，不许凭印象编。\n"
            "本工具只查资料，不抽牌；抽牌另走 tarot_divine。"
        ),
        parameters=[
            ToolParameterInfo(
                name="name",
                param_type=ToolParamType.STRING,
                description="神名或问题原话",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def deity_lore_tool(self, name: str = "", **kwargs: Any) -> dict[str, Any]:
        card, hit = self._find(name or "")
        if card:
            return {"success": True, "message": self._card_text(card)}
        if hit:
            return {
                "success": True,
                "message": (
                    f"【本地神祇库没有「{hit}」的条目】\n"
                    "回答前必须先调用 web_search 查这个神的可靠资料（优先原始文献或 Theoi 一类汇纂），"
                    "拿到依据再开口；查不到就直说不知道，不许凭印象编。"
                ),
            }
        return {
            "success": True,
            "message": "本地神祇库没有匹配的神名。若这是一个真实神祇，先调用 web_search 查证再回答。",
        }

    @HookHandler(
        "maisaka.replyer.before_request",
        name="deity_lore_inject",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_deity_lore(
        self,
        extra_prompt: str = "",
        session_id: str = "",
        reply_message_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not reply_message_id:
            return {"action": "continue"}
        text = ""
        try:
            data = await self.ctx.message.get_by_id(reply_message_id, stream_id=session_id or "")
            text = _extract_text(data)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("取神祇目标消息失败: %s", exc)
        card, hit = self._find(text)
        if card:
            block = (
                "\n\n【神祇依据 · 不外显】\n"
                + self._brief_text(card)
                + "\n用法：最多引用一点点，和当前话题接上；说成像群里随口一句话，"
                "不要罗列职能、象征、圣号、出处，不要背词条；"
                "敬畏不等于变怂，仍然是呆猫的短句。"
            )
        elif hit:
            block = (
                f"\n\n【神祇话题：本地没有「{hit}」的资料】\n"
                "不要编祂的脾气和私事。如果上文已有搜索资料，只挑一句最要紧的说，"
                "不要复述资料、不要罗列；不确定就承认不清楚。"
                "敬畏不等于背词条，仍然是短句、仍然是呆猫。"
            )
        else:
            return {"action": "continue"}
        merged = dict(kwargs)
        merged["extra_prompt"] = (extra_prompt or "") + block
        return {"modified_kwargs": merged}


def create_plugin() -> DeityLorePlugin:
    return DeityLorePlugin()
