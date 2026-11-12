"""小亚细亚五羊距骨神谕（Astragalomancy · MaiBot 显式入口版 v0.1.0）

依据公元二世纪西南小亚细亚（Kremna IK 57 no. 5 / Termessos TAM III,1 34-35 等）
市集石柱 56 签神谕碑铭：
- 投掷 5 颗真实绵羊/山羊距骨（仅四面可落定：1=Χῖος 希俄斯窄面、3=ὕπτιον 仰卧凹宽面、
  4=πρανές 俯卧凸宽面、6=Κῷος 科斯 S 沟窄面，无 2 与 5）；
- 默认采用 F.N. David 实测羊距骨物理权重 {1: 0.10, 3: 0.40, 4: 0.40, 6: 0.10}，
  共 C(4+5-1, 5) = 56 种无序骨相（总点数 5~30，6 与 29 物理不可达，同分异构各归不同神签）；
- 联动补丁包内的神祇卡，严守敬神准则；
- 当前仅开放显式入口（/距骨、/羊距骨、/骨签 或点名向呆猫求掷羊距骨），不注册通用 Planner Tool，
  不抢日常塔罗星骰+梅花二则的分流。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

MAIBOT_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path.cwd()).expanduser().resolve()
RESOURCE_ROOT = MAIBOT_ROOT / "data" / "daimon-resources"


def _configured_path(value: str | Path) -> Path:
    """相对路径按实例目录解析，绝对路径原样使用。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else MAIBOT_ROOT / path


DEFAULT_DATA_PATH = RESOURCE_ROOT / "astragaloi" / "asia_minor_56.json"
DEFAULT_DEITY_DIR = RESOURCE_ROOT / "deity-cards"
DEFAULT_STATE_PATH = MAIBOT_ROOT / "temp" / "astragaloi_guard.json"

DEFAULT_FACE_WEIGHTS: dict[int, float] = {1: 0.10, 3: 0.40, 4: 0.40, 6: 0.10}
FACE_SHORT_ZH: dict[int, str] = {
    1: "希俄斯窄(1)",
    3: "仰凹宽(3)",
    4: "俯凸宽(4)",
    6: "科斯沟(6)",
}

# 学术/配置元讨论不触发求签
META_WORDS = (
    "插件", "配置", "设置", "调试", "代码", "怎么用", "是什么", "什么意思",
    "考古", "文献", "论文", "历史上", "古人怎么", "碑铭原文",
)
BONE_NOUNS = ("羊距骨", "距骨神谕", "距骨签", "距骨", "踝骨签", "骨签")
CAST_VERBS = ("掷", "摇", "扔", "抛", "求", "占", "算", "来一", "抽", "看看", "问问", "帮我", "替我")
BOT_NAMES = ("呆猫", "小苗")

# MaiBot 会把被回复的那条消息塞进 message_text/plain_text 开头，形如
# "[回复了某人的消息: <原文>] <本人说的话>"。引用里常带"距骨/骨签"，直接拿去匹配
# 会被当成新求签请求，拿引用文本当题干。这里只保留本人说的话。
_QUOTE_PREFIX_RE = re.compile(r"^\s*\[回复了[^\n]*?\]\s+")


# ── 距骨求问守门：话题状态 / 同题拒杯 / 成功配额 ─────────────
GUARD_VERSION = 3
HOUR_SECONDS = 3600.0

CUP_MEANING = {
    "圣杯": "准问",
    "笑杯": "笑而不答：话没问清，或者你心里早有答案",
    "阴杯": "不允：此刻不问，问了也不准",
}

REPLY = {
    "suspended": "今天石柱不接你了——这题连着三次摇头，明天再来。",
    "hourly": "这个钟头问得太密了，神明不兴这么催，过一个钟头再来。",
    "daily": "今天问够了喵，改天再来。",
    "gap": "刚答过一签，还没凉呢，缓一下再问。",
    "busy": "上一签还在解，等我一下喵。",
    "clarify": "老大这句跟刚才那件是一件事吗？回我「还是那件」或「换一件」。",
    "ask_new": "那说新的：/距骨 你要问的那件事。",
    "repeat": "还是刚才那一签：",
}

SAME_HINT = ("同一件", "还是那件", "还是那个", "就是刚才", "就是那个", "同一桩", "还是这件事", "同一题")
OTHER_HINT = ("换一件", "换一个", "另一件", "另一个", "不是", "新的", "换件事", "别的事", "换题")

TOPIC_MATCH_PROMPT = """判断两次求签是不是同一件事。

上一次问：{old}
这一次问：{new}

规则：
- 对象（人 / 店 / 项目 / 关系）、时间、条件都没变 → same
- 对象、时间或条件变了（“这周”变“下周”、“A 店”变“B 店”）→ no
- 只是补了背景、换了说法、加了细节，事情还是那一件 → same
- 看不出来 → unsure

只输出 JSON：{{"same": "same|no|unsure"}}"""


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _norm_text(text: str) -> str:
    return re.sub(r"[\s，。,.！!？?~～、；;：:…\-—「」『』\"'（）()]+", "", str(text or "")).lower()


def _similarity(a: str, b: str) -> float:
    try:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio()
    except Exception:  # noqa: BLE001
        return 0.0


def _extract_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


class _NullLock:
    """占位锁：已经在持有 guard.lock() 时复用同一临界区。"""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


_NULL_LOCK = _NullLock()

class GuardStore:
    """一份 JSON、单进程串行；读改写都在同一把 asyncio.Lock 里。"""

    def __init__(self, path_getter):
        self._path_getter = path_getter
        self._lock: asyncio.Lock | None = None
        self._loop = None

    def lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path_getter().read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
        return {"version": GUARD_VERSION, "users": {}}

    def save(self, state: dict[str, Any]) -> bool:
        """写盘成功返回 True；失败必须让调用方看见，不能假装存好了。"""
        path = self._path_getter()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:  # noqa: BLE001
            return False


def user_rec(state: dict[str, Any], uid: str) -> dict[str, Any]:
    users = state.setdefault("users", {})
    rec = users.get(uid)
    if not isinstance(rec, dict):
        rec = {}
    if rec.get("day") != _today():
        # 跨日：成功次数归零，同题拒杯也重新给三次机会；原签与窗口记录保留
        rec["day"] = _today()
        rec["day_success"] = 0
        for topic in (rec.get("topics") or {}).values():
            if isinstance(topic, dict):
                topic["strikes"] = 0
    rec.setdefault("successes", [])
    rec.setdefault("last_success", 0.0)
    rec.setdefault("suspended_day", "")
    rec.setdefault("topics", {})
    rec.setdefault("clarify", None)
    users[uid] = rec
    return rec


def prune_rec(rec: dict[str, Any], now: float, cfg: Any) -> None:
    """滚动一小时按小时剪；已完成的话题按 6 小时窗口剪，未完成的原签一直保留。"""
    rec["successes"] = [float(t) for t in (rec.get("successes") or []) if now - float(t) < HOUR_SECONDS]
    window = float(getattr(cfg, "answered_window_hours", 6) or 6) * HOUR_SECONDS
    for tid, topic in list((rec.get("topics") or {}).items()):
        if not isinstance(topic, dict):
            del rec["topics"][tid]
            continue
        if topic.get("status") == "casting":
            continue  # 没发出去的原签不随窗口过期
        if now - float(topic.get("last_seen") or 0.0) > window:
            del rec["topics"][tid]


def quota_code(rec: dict[str, Any], now: float, cfg: Any) -> str:
    if len(rec.get("successes") or []) >= int(getattr(cfg, "success_hourly", 5)):
        return "hourly"
    if int(rec.get("day_success") or 0) >= int(getattr(cfg, "success_daily", 12)):
        return "daily"
    if now - float(rec.get("last_success") or 0.0) < float(getattr(cfg, "success_gap_seconds", 30)):
        return "gap"
    return ""


def new_topic_id(rec: dict[str, Any], text: str, now: float) -> str:
    salt = 0
    tid = "t_" + hashlib.md5(f"{text}|{now}".encode("utf-8")).hexdigest()[:8]
    while tid in rec.get("topics", {}):
        salt += 1
        tid = "t_" + hashlib.md5(f"{text}|{now}|{salt}".encode("utf-8")).hexdigest()[:8]
    return tid


def roll_binary_jiaobei(rng: random.Random | None = None) -> dict[str, Any]:
    """独立掷两枚二进制位：1=阳 / 0=阴；圣 50%，笑 25%，阴 25%。"""
    r = rng or random.SystemRandom()
    b1, b2 = r.randint(0, 1), r.randint(0, 1)
    if b1 != b2:
        cup = "圣杯"
    else:
        cup = "笑杯" if b1 == 1 else "阴杯"
    return {"bits": [b1, b2], "bits_str": f"{b1}/{b2}", "cup": cup}


def _user_id_of(message: dict[str, Any], fallback: str = "") -> str:
    info = message.get("message_info") or {}
    user = info.get("user_info") or {}
    return str(user.get("user_id") or "") or fallback


def strip_quote_prefix(text: str) -> str:
    """剥掉“回复了某人的消息: …”引用前缀，只留提问者自己说的话。"""
    clean = str(text or "")
    for _ in range(3):
        trimmed = _QUOTE_PREFIX_RE.sub("", clean, count=1)
        if trimmed == clean:
            break
        clean = trimmed
    return clean.strip()


def _int_to_roman(num: int) -> str:
    val = [
        (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
        (5, "V"), (4, "IV"), (1, "I"),
    ]
    out: list[str] = []
    n = max(1, int(num))
    for v, sym in val:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


def _command_text(kwargs: dict[str, Any]) -> str:
    """从 Command 调用参数里取正文。

    Host 的 Command 执行器传的是 ``text``（完整 processed_plain_text）与
    ``message`` 字典；只有旧式调用才会给 message_text/args 这些键。
    """
    value = kwargs.get("text")
    if isinstance(value, str) and value.strip():
        return value.strip()
    message = kwargs.get("message")
    if isinstance(message, dict):
        for key in ("processed_plain_text", "plain_text", "raw_message"):
            candidate = message.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    for key in ("message_text", "processed_plain_text", "plain_text", "raw_message", "args", "content"):
        candidate = kwargs.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def is_explicit_astragaloi_request(text: str, *, is_private: bool = False, is_mentioned: bool = False) -> bool:
    """仅识别明确要掷羊距骨的显式请求，避免学术闲聊误触发。"""
    clean = strip_quote_prefix(text)
    if not clean:
        return False
    if re.match(r"^\s*/(?:距骨|羊距骨|距骨神谕|骨签)(?:\s+.*)?$", clean):
        return True
    if any(w in clean for w in META_WORDS):
        return False
    if not any(n in clean for n in BONE_NOUNS):
        return False
    if not any(v in clean for v in CAST_VERBS):
        return False
    # 群聊中要求带 @提及、点名呆猫/小苗，或直接以祈使动作开头（如“掷把羊距骨看看……”）
    if is_private or is_mentioned or any(b in clean for b in BOT_NAMES) or "@" in clean or "＠" in clean:
        return True
    stripped = re.sub(r"^(?:请|麻烦|帮我|替我)?\s*", "", clean)
    return stripped.startswith(("掷", "摇", "扔", "抛", "来一把距骨", "来一把羊距骨", "来一签距骨", "来一签羊距骨", "求一签距骨", "求一签羊距骨"))


def roll_five_astragaloi(
    *,
    use_real_weights: bool = True,
    weights_map: dict[int, float] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """独立投掷 5 颗四面羊距骨，返回原始落序与升序组合键。"""
    r = rng or random.SystemRandom()
    faces = [1, 3, 4, 6]
    if use_real_weights:
        wmap = weights_map or DEFAULT_FACE_WEIGHTS
        weights = [float(wmap.get(f, DEFAULT_FACE_WEIGHTS[f])) for f in faces]
    else:
        weights = [0.25, 0.25, 0.25, 0.25]

    # SystemRandom 未内置 choices 权重优化时用精确累积分布采样
    total_w = sum(weights)
    raw_bones: list[int] = []
    for _ in range(5):
        u = r.random() * total_w
        acc = 0.0
        picked = faces[-1]
        for f, w in zip(faces, weights):
            acc += w
            if u <= acc:
                picked = f
                break
        raw_bones.append(picked)

    sorted_bones = sorted(raw_bones)
    key = "-".join(str(b) for b in sorted_bones)
    return {
        "raw_bones": raw_bones,
        "bones": sorted_bones,
        "key": key,
        "sum": sum(sorted_bones),
    }


def format_throw_line(throw_entry: dict[str, Any], roll_info: dict[str, Any]) -> str:
    """石柱签头一行：骨相、总点数、碑铭签序、冠名神祇与吉凶判目。"""
    idx = int(throw_entry.get("index") or 0)
    roman = _int_to_roman(idx) if idx > 0 else "?"
    key = str(roll_info.get("key") or "-".join(str(x) for x in throw_entry.get("bones", [])))
    total = int(throw_entry.get("sum") or roll_info.get("sum") or 0)
    deity_zh = str(throw_entry.get("deity_zh") or "未知神签")
    deity_grc = str(throw_entry.get("deity_grc") or "")
    verdict = str(throw_entry.get("verdict") or "").strip()
    prob = float(throw_entry.get("prob_real") or 0.0)

    rare_tag = ""
    if 0 < prob <= 0.0005:
        rare_tag = " · 极罕神骨"
    elif 0 < prob <= 0.0025:
        rare_tag = " · 罕见骨相"

    grc_part = f"（{deity_grc}）" if deity_grc else ""
    verdict_part = f" — {verdict}" if verdict else ""
    return f"五羊距骨 · {key}（总 {total} · 第 {roman} 签{rare_tag}）｜{deity_zh}{grc_part}{verdict_part}"


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用五羊距骨神谕插件")
    jiaobei_gate: bool = Field(default=True, description="掷骨前先掷二进制茭杯，非圣杯不掷")
    state_path: str = Field(default="", description="守门账本路径；留空用默认 temp/astragaloi_guard.json")
    success_hourly: int = Field(default=5, description="每人每滚动一小时最多成功答几签")
    success_daily: int = Field(default=12, description="每人每天最多成功答几签")
    success_gap_seconds: int = Field(default=30, description="同一人两签之间的最小间隔秒数")
    topic_strike_limit: int = Field(default=3, description="同一件事连续几次杯拒后暂停此人当天求问")
    answered_window_hours: int = Field(default=6, description="已答问题保留并复用多少小时")
    topic_similarity_floor: float = Field(default=0.45, description="低于此相似度直接当新题；高于则交给模型判定")
    topic_match_task: str = Field(default="replyer", description="同题判定使用的任务名")
    topic_match_model: str = Field(default="deepseek-flash", description="同题判定使用的模型")
    asker_exempt_user_ids: list[str] = Field(
        default_factory=list,
        description="豁免名单：不计成功配额与当天暂停，但仍记录原签（公开版默认空）",
    )
    data_path: str = Field(
        default=str(DEFAULT_DATA_PATH),
        description="56 签小亚细亚碑铭数据包路径（asia_minor_56.json）",
    )
    deity_dir: str = Field(
        default=str(DEFAULT_DEITY_DIR),
        description="本地希腊神祇资料目录（含 cards.json 与 catalog.json）",
    )
    use_real_bone_weights: bool = Field(
        default=True,
        description="使用真实羊距骨四面物理概率（1:10%, 3:40%, 4:40%, 6:10%）；关闭则四面等概",
    )
    digest: bool = Field(default=True, description="用模型把碑铭判词消化成呆猫口吻；关闭则直出碑铭译文")
    digest_task: str = Field(default="replyer", description="消化使用的模型任务名")
    digest_model: str = Field(default="deepseek-flash", description="消化直接指定的模型名；留空用任务默认")
    digest_max_tokens: int = Field(default=800, description="消化最大输出 token")
    show_line: bool = Field(default=True, description="先单独发一行石柱骨相与神名签头")
    min_chars: int = Field(default=80, description="断语最短字数")
    max_chars: int = Field(default=150, description="断语最长字数")
    config_version: str = Field(default="0.2.0", description="配置版本")


class AstragaloiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class AstragaloiPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = AstragaloiConfig

    def __init__(self) -> None:
        super().__init__()
        self._pack: dict[str, Any] = {}
        self._throws: dict[str, dict[str, Any]] = {}
        self._weights: dict[int, float] = dict(DEFAULT_FACE_WEIGHTS)
        self._deity_cards: dict[str, dict[str, Any]] = {}
        self._deity_catalog: dict[str, dict[str, Any]] = {}
        # session_id -> 该会话最近一次掷骨 {q_hash, question, ts, roll, entry, digest}
        self._guard: GuardStore | None = None
        self._finishing: set[str] = set()
        self._unsaved: dict[str, dict[str, Any]] = {}

    async def on_load(self) -> None:
        self._reload_resources()
        self.ctx.logger.info(
            "小亚细亚五羊距骨神谕 v0.1.0 已加载（签数=%d，神祇卡=%d，显式入口模式）",
            len(self._throws),
            len(self._deity_cards),
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str = "", config_data: dict | None = None, version: str = "") -> None:
        self._reload_resources()
        return None

    def _get_cfg(self) -> PluginSection:
        try:
            return self.config.plugin
        except Exception:  # noqa: BLE001
            return PluginSection()

    def _reload_resources(self) -> None:
        cfg = self._get_cfg()
        data_path = Path(str(getattr(cfg, "data_path", DEFAULT_DATA_PATH) or DEFAULT_DATA_PATH))
        deity_dir = Path(str(getattr(cfg, "deity_dir", DEFAULT_DEITY_DIR) or DEFAULT_DEITY_DIR))

        if data_path.exists():
            try:
                raw = json.loads(data_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("throws"), dict):
                    self._pack = raw
                    self._throws = raw["throws"]
                    fw = raw.get("face_weights_real")
                    if isinstance(fw, dict):
                        parsed_w = {int(k): float(v) for k, v in fw.items() if int(k) in (1, 3, 4, 6)}
                        if len(parsed_w) == 4:
                            self._weights = parsed_w
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("读取五羊距骨碑铭库失败: %s", exc)

        cards_path = deity_dir / "cards.json"
        catalog_path = deity_dir / "catalog.json"
        if cards_path.exists():
            try:
                cards = json.loads(cards_path.read_text(encoding="utf-8"))
                if isinstance(cards, dict):
                    self._deity_cards = cards
            except Exception:  # noqa: BLE001
                pass
        if catalog_path.exists():
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                if isinstance(catalog, list):
                    self._deity_catalog = {
                        str(item["slug"]): item
                        for item in catalog
                        if isinstance(item, dict) and item.get("slug")
                    }
            except Exception:  # noqa: BLE001
                pass

    def _deity_context_line(self, throw_entry: dict[str, Any]) -> str:
        """从本地希腊神祇库提取极简神格锚点，供消化时守住敬畏、不编排神明。"""
        slug = str(throw_entry.get("base_deity_slug") or "").strip()
        note = str(throw_entry.get("deity_note") or "").strip()
        parts: list[str] = []
        if slug and slug in self._deity_cards:
            card = self._deity_cards[slug]
            summary = str(card.get("summary") or "").strip()
            first = re.split(r"[。；;]", summary)[0] if summary else ""
            if first:
                parts.append(first)
            domains = card.get("domain") or []
            if domains:
                parts.append("司掌：" + "、".join(str(x) for x in domains[:3]))
        elif slug and slug in self._deity_catalog:
            item = self._deity_catalog[slug]
            parts.append(f"希腊神祇名录：{item.get('zh', '')}（{item.get('en', '')} / {item.get('grc', '')}）")
        if note:
            parts.append(note)
        return "；".join(p for p in parts if p)

    # ── 显式入口 1：斜杠命令 /距骨 /羊距骨 /骨签 ──────────────
    @Command(
        "astragaloi",
        description="/距骨 <问题> 掷五颗小亚细亚羊距骨问石柱神谕",
        pattern=r"^\s*(?:[@＠]\S+\s*)*/(?:距骨|羊距骨|距骨神谕|骨签)(?:\s+.*)?$",
    )
    async def handle_command(self, stream_id: str = "", **kwargs: Any) -> tuple:
        text = _command_text(kwargs)
        text = strip_quote_prefix(text)
        question = re.sub(r"^\s*(?:[@＠]\S+\s*)*/(?:距骨|羊距骨|距骨神谕|骨签)\s*", "", text).strip(" ，。,.")
        message = kwargs.get("message") or {}
        uid = str(kwargs.get("user_id") or _user_id_of(message))

        clarify_state, clarify = await self._consume_clarify(stream_id, uid, question)
        if clarify_state == "same":
            asyncio.create_task(
                self._handle_request(
                    stream_id,
                    str(clarify.get("pending_text") or ""),
                    uid,
                    str(clarify.get("pending_msg_id") or ""),
                    str(clarify.get("pending_reply_to") or ""),
                    force_topic_id=str(clarify.get("topic_id") or ""),
                )
            )
            return True, "已续问上一件", True
        if clarify_state == "other":
            await self._safe_say(REPLY["ask_new"], stream_id)
            return True, "等新问题", True

        asyncio.create_task(
            self._handle_request(
                stream_id,
                question,
                uid,
                str(message.get("message_id") or ""),
                str(message.get("reply_to") or ""),
            )
        )
        return True, "已掷五羊距骨", True

    # ── 显式入口 2：点名向呆猫求掷羊距骨的自然语言钩子 ──────────
    @HookHandler(
        hook="chat.receive.after_process",
        name="astragaloi_explicit_trigger",
        description="检测显式的五羊距骨求签请求",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_explicit_text(self, **kwargs: Any) -> dict | None:
        message: dict[str, Any] = kwargs.get("message", {}) or {}
        if message.get("is_command"):
            return None
        text = strip_quote_prefix(
            str(
                message.get("processed_plain_text", "")
                or message.get("plain_text", "")
                or message.get("raw_message", "")
                or ""
            )
        )
        if not text:
            return None
        stream_id = str(message.get("session_id", "") or "")
        if not stream_id:
            return None
        uid = _user_id_of(message)

        is_private = bool(message.get("is_private")) or ("private" in stream_id.lower())
        is_mentioned = bool(message.get("is_mentioned") or message.get("is_at"))
        is_request = is_explicit_astragaloi_request(text, is_private=is_private, is_mentioned=is_mentioned)

        # 先看是不是在回上一句澄清：短句不进普通触发判断
        clarify_state, clarify = await self._consume_clarify(
            stream_id, uid, text, is_request=is_request
        )
        if clarify_state == "same":
            asyncio.create_task(
                self._handle_request(
                    stream_id,
                    str(clarify.get("pending_text") or ""),
                    uid,
                    str(clarify.get("pending_msg_id") or ""),
                    str(clarify.get("pending_reply_to") or ""),
                    force_topic_id=str(clarify.get("topic_id") or ""),
                )
            )
            return {"action": "abort"}
        if clarify_state == "other":
            asyncio.create_task(self._safe_say(REPLY["ask_new"], stream_id))
            return {"action": "abort"}

        if not is_request:
            return None

        question = re.sub(r"^(?:[@＠]\S+\s*)+", "", text)
        for w in ("呆猫", "小苗", "帮我", "替我", "麻烦", "请", "掷一把", "摇一把", "扔一把", "来一把", "来一签", "求一签", "羊距骨", "距骨神谕", "距骨签", "距骨", "骨签"):
            question = question.replace(w, "")
        question = question.strip(" ，。,.？！?!")
        asyncio.create_task(
            self._handle_request(
                stream_id,
                question,
                uid,
                str(message.get("message_id") or ""),
                str(message.get("reply_to") or ""),
            )
        )
        return {"action": "abort"}

    # ── 核心投掷与消化 ─────────────────────────────────────
    async def _say(self, text: str, stream_id: str) -> None:
        """发一条用户可见的话，并同步进 Maisaka 历史。

        插件直发默认不进 Maisaka 上下文，呆猫下一轮会完全看不见自己说过什么，
        被引用时只能答"前面没看着"。这里显式同步，并标成自己的发言。
        """
        await self.ctx.send.text(
            str(text),
            stream_id,
            sync_to_maisaka_history=True,
            maisaka_source_kind="guided_reply",
        )

    # ── 核心：话题状态机 ───────────────────────────────────
    async def _say_ok(self, text: str, stream_id: str) -> tuple[bool, str]:
        """发送并返回是否成功、平台消息 ID。"""
        if not stream_id:
            return False, ""
        try:
            result = await self.ctx.send.text(
                str(text),
                stream_id,
                return_details=True,
                sync_to_maisaka_history=True,
                maisaka_source_kind="guided_reply",
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("距骨发送失败: %s", exc)
            return False, ""
        if isinstance(result, dict):
            return bool(result.get("sent")), str(result.get("message_id") or "")
        return bool(result), ""

    async def _safe_say(self, text: str, stream_id: str) -> None:
        await self._say_ok(text, stream_id)

    def _state_path(self) -> Path:
        raw = str(getattr(self._get_cfg(), "state_path", "") or "").strip()
        return Path(raw) if raw else DEFAULT_STATE_PATH

    def _guard_store(self) -> GuardStore:
        if self._guard is None:
            self._guard = GuardStore(self._state_path)
        return self._guard

    async def _handle_request(
        self,
        stream_id: str,
        question: str,
        user_id: str,
        message_id: str = "",
        reply_to: str = "",
        force_topic_id: str = "",
    ) -> str:
        cfg = self._get_cfg()
        if not getattr(cfg, "enabled", True):
            return ""
        if not self._throws:
            self._reload_resources()
        if not self._throws:
            msg = "装羊距骨的口袋还没扎好（56 签碑铭库未就绪），等一下再来掷喵。"
            await self._safe_say(msg, stream_id)
            return msg

        q_text = strip_quote_prefix(question).strip(" ，。,.！!？?~～、")
        if not q_text:
            msg = "老大要问哪桩事？把事说清楚我再掷（/距骨 问题）——空着摇，石柱不知道你在问什么喵。"
            await self._safe_say(msg, stream_id)
            return msg

        uid = str(user_id or "").strip()
        if not uid:
            # 取不到真实发送者时不能拿会话号顶人：拒答并留日志
            self.ctx.logger.warning("距骨求问缺少提问者 ID，已拒绝: stream_id=%s text=%s", stream_id, q_text[:40])
            msg = "没认出是谁在问，这一签先不掷。等账号对上了再来。"
            await self._safe_say(msg, stream_id)
            return msg

        guard = self._guard_store()

        async with guard.lock():
            state = guard.load()
            rec = user_rec(state, uid)
            now = time.time()
            prune_rec(rec, now, cfg)
            exempt = uid in {str(x) for x in (getattr(cfg, "asker_exempt_user_ids", []) or [])}

            # 上次落盘失败压住的原签：先补写，再继续，不重掷
            stuck = self._unsaved.get(uid)
            if isinstance(stuck, dict):
                rec.setdefault("topics", {})[str(stuck.get("id") or "")] = stuck
                if guard.save(state):
                    self._unsaved.pop(uid, None)
                else:
                    self.ctx.logger.error("距骨账本仍然写不进去，原签继续压在内存: uid=%s", uid)
                    msg = "账册还是没誊进去，这一签先搁着，别急着再摇。"
                    await self._safe_say(msg, stream_id)
                    return msg

            if not exempt and rec.get("suspended_day") == _today():
                guard.save(state)
                await self._safe_say(REPLY["suspended"], stream_id)
                return REPLY["suspended"]

            forced = str(force_topic_id or "").strip()
            if forced and forced in (rec.get("topics") or {}):
                action, tid, topic = "same", forced, rec["topics"][forced]
                topic["last_seen"] = now
                if message_id:
                    topic["question_msg_id"] = str(message_id)
            else:
                action, tid, topic = await self._resolve_topic(rec, q_text, reply_to, cfg, now)

            if action == "clarify":
                rec["clarify"] = {
                    "topic_id": tid,
                    "asked_at": now,
                    "session_id": stream_id,
                    "pending_text": q_text,
                    "pending_msg_id": str(message_id or ""),
                    "pending_reply_to": str(reply_to or ""),
                }
                guard.save(state)
                await self._safe_say(REPLY["clarify"], stream_id)
                return REPLY["clarify"]

            if action == "same" and isinstance(topic, dict):
                if topic.get("status") == "answered":
                    topic["last_seen"] = now
                    guard.save(state)
                    msg = f"{REPLY['repeat']}{topic.get('line', '')}\n{topic.get('digest', '')}".strip()
                    await self._safe_say(msg, stream_id)
                    return msg
                if topic.get("status") == "casting":
                    topic["last_seen"] = now
                    guard.save(state)
                    if tid in self._finishing:
                        await self._safe_say(REPLY["busy"], stream_id)
                        return REPLY["busy"]
                    return await self._finish_topic(stream_id, uid, tid, cfg, locked=True)

            if action == "new" or not isinstance(topic, dict):
                tid = new_topic_id(rec, q_text, now)
                topic = {
                    "id": tid,
                    "text": q_text,
                    "norm": _norm_text(q_text),
                    "created": now,
                    "last_seen": now,
                    "status": "open",
                    "strikes": 0,
                    "question_msg_id": str(message_id or ""),
                    "sign_msg_id": "",
                    "line": "",
                    "digest": "",
                    "answer": "",
                    "answered_at": 0.0,
                    "cast_at": 0.0,
                    "sent_line": False,
                    "sent_digest": False,
                    "sign": None,
                }
                rec.setdefault("topics", {})[tid] = topic
            else:
                topic["text"] = q_text
                topic["norm"] = _norm_text(q_text)
                topic["last_seen"] = now
                if message_id:
                    topic["question_msg_id"] = str(message_id)

            # 别的题还在解：不让同一人并行开第二签
            busy = any(
                isinstance(other, dict)
                and other.get("id") != tid
                and other.get("status") == "casting"
                and now - float(other.get("cast_at") or 0.0) < 600
                for other in rec.get("topics", {}).values()
            )
            if busy:
                guard.save(state)
                await self._safe_say(REPLY["busy"], stream_id)
                return REPLY["busy"]

            if not exempt:
                code = quota_code(rec, now, cfg)
                if code:
                    guard.save(state)
                    msg = REPLY.get(code, REPLY["daily"])
                    await self._safe_say(msg, stream_id)
                    return msg

            cup = roll_binary_jiaobei() if getattr(cfg, "jiaobei_gate", True) else None
            if cup and cup["cup"] != "圣杯":
                strikes = int(topic.get("strikes") or 0) + 1
                topic["strikes"] = strikes
                topic["last_seen"] = now
                limit = int(getattr(cfg, "topic_strike_limit", 3))
                hit_limit = (not exempt) and strikes >= limit
                if hit_limit:
                    rec["suspended_day"] = _today()
                guard.save(state)
                tail = (
                    f"这题连着 {strikes} 次摇头了，今天先停，明天再来。"
                    if hit_limit
                    else "把话问清楚再来，换措辞也行。"
                )
                msg = f"先掷杯：{cup['bits_str']} · {cup['cup']}——{CUP_MEANING[cup['cup']]}。{tail}"
                await self._safe_say(msg, stream_id)
                return msg

            if cup:
                topic["strikes"] = 0

            roll_info = roll_five_astragaloi(
                use_real_weights=bool(getattr(cfg, "use_real_bone_weights", True)),
                weights_map=self._weights,
            )
            throw_entry = self._throws.get(roll_info["key"])
            if not throw_entry:
                guard.save(state)
                msg = f"五颗骨头落出了 {roll_info['key']}，但石柱拓片没查到对应行，先不乱断。"
                await self._safe_say(msg, stream_id)
                return msg

            line = format_throw_line(throw_entry, roll_info)
            if cup:
                line = f"〔杯 {cup['bits_str']} · 圣〕{line}"
            topic.update(
                {
                    "status": "casting",
                    "cast_at": now,
                    "last_seen": now,
                    "sign": {"roll": roll_info, "entry": throw_entry},
                    "line": line,
                    "digest": "",
                    "sent_line": False,
                    "sent_digest": False,
                    "strikes": 0,
                }
            )
            tid = str(topic.get("id") or tid)
            if not guard.save(state):
                # 起卦后必须立即落盘；写不进去就压内存、停止结算，下次接着这份结果
                self._unsaved[uid] = dict(topic)
                self.ctx.logger.error("距骨起卦后账本落盘失败，原签暂存内存: uid=%s tid=%s", uid, tid)
                msg = "石柱把这一签记下了，可账册没誊进去。先搁着，我待会儿再誊，别重摇。"
                await self._safe_say(msg, stream_id)
                return msg

        if tid in self._finishing:
            await self._safe_say(REPLY["busy"], stream_id)
            return REPLY["busy"]
        return await self._finish_topic(stream_id, uid, tid, cfg)

    async def _consume_clarify(
        self, stream_id: str, uid: str, text: str, *, is_request: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """在入口先看有没有待确认的问题；短句「还是那件 / 换一件」在这里被接住。"""
        uid = str(uid or "").strip()
        if not uid or not stream_id:
            return "", {}
        guard = self._guard_store()
        async with guard.lock():
            state = guard.load()
            rec = user_rec(state, uid)
            clarify = rec.get("clarify")
            if not isinstance(clarify, dict):
                return "", {}
            if str(clarify.get("session_id") or "") != stream_id:
                return "", {}
            if time.time() - float(clarify.get("asked_at") or 0.0) > 300:
                rec["clarify"] = None
                guard.save(state)
                return "", {}
            short = str(text or "").strip()
            # “不是同一件”同时含两组提示词，先处理明确否定／换题。
            if len(short) <= 10 and any(word in short for word in OTHER_HINT):
                rec["clarify"] = None
                guard.save(state)
                return "other", clarify
            if len(short) <= 10 and any(word in short for word in SAME_HINT):
                rec["clarify"] = None
                guard.save(state)
                return "same", clarify
            if is_request:
                # 直接抛出新问题：旧的待确认作废
                rec["clarify"] = None
                guard.save(state)
                return "none", clarify
            # 其余闲聊不打扰，澄清留到 5 分钟窗口内还能答
            return "", {}

    async def _resolve_topic(
        self,
        rec: dict[str, Any],
        q_text: str,
        reply_to: str,
        cfg: Any,
        now: float,
    ) -> tuple[str, str, dict[str, Any] | None]:
        topics = rec.setdefault("topics", {})
        norm = _norm_text(q_text)

        # 0) 澄清回话：只认很短的一句
        clarify = rec.get("clarify")
        if isinstance(clarify, dict) and now - float(clarify.get("asked_at") or 0.0) < 300:
            short = q_text.strip()
            if len(short) <= 10:
                if any(word in short for word in SAME_HINT):
                    rec["clarify"] = None
                    tid = str(clarify.get("topic_id") or "")
                    if tid in topics:
                        topics[tid]["last_seen"] = now
                        return "same", tid, topics[tid]
                elif any(word in short for word in OTHER_HINT):
                    rec["clarify"] = None
                    return "new", "", None
        rec["clarify"] = None

        # 1) 明确回复了某道题或某支签
        target = str(reply_to or "").strip()
        if target:
            for tid, topic in topics.items():
                if target in (str(topic.get("question_msg_id") or ""), str(topic.get("sign_msg_id") or "")):
                    topic["last_seen"] = now
                    return "same", tid, topic

        # 2) 规范化后完全相同
        for tid, topic in topics.items():
            if norm and str(topic.get("norm") or "") == norm:
                topic["last_seen"] = now
                return "same", tid, topic

        # 3) 近似只当候选，是否同题交给模型；拿不准就问一句
        best_tid, best_score, best_topic = "", 0.0, None
        for tid, topic in topics.items():
            old = str(topic.get("norm") or "")
            if not old:
                continue
            score = _similarity(old, norm)
            if score > best_score:
                best_tid, best_score, best_topic = tid, score, topic

        floor = float(getattr(cfg, "topic_similarity_floor", 0.45))
        if isinstance(best_topic, dict) and best_score >= floor:
            verdict = await self._ask_same_topic(str(best_topic.get("text") or ""), q_text, cfg)
            if verdict == "same":
                best_topic["last_seen"] = now
                return "same", best_tid, best_topic
            if verdict == "unsure":
                return "clarify", best_tid, best_topic
        return "new", "", None

    async def _ask_same_topic(self, old: str, new: str, cfg: Any) -> str:
        prompt = TOPIC_MATCH_PROMPT.format(old=old[:120], new=new[:120])
        try:
            result = await self.ctx.call_capability(
                "llm.generate",
                timeout_ms=20000,
                prompt=prompt,
                task_name=str(getattr(cfg, "topic_match_task", "replyer") or "replyer"),
                model_name=str(getattr(cfg, "topic_match_model", "") or ""),
                temperature=0.0,
                max_tokens=60,
            )
            raw = str((result or {}).get("response") or "")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("同题判定调用失败，按拿不准处理（走澄清，不继承旧题）: %s", exc)
            return "unsure"
        value = str(_extract_json(raw).get("same") or "").strip().lower()
        if value in ("same", "yes", "true"):
            return "same"
        if value in ("no", "false", "different"):
            return "no"
        return "unsure"

    async def _finish_topic(
        self, stream_id: str, uid: str, tid: str, cfg: Any, *, locked: bool = False
    ) -> str:
        """同一签只允许一个结算流程；重复进入直接挡回。"""
        if tid in self._finishing:
            await self._safe_say(REPLY["busy"], stream_id)
            return REPLY["busy"]
        self._finishing.add(tid)
        try:
            return await self._finish_topic_inner(stream_id, uid, tid, cfg, locked=locked)
        finally:
            self._finishing.discard(tid)

    async def _finish_topic_inner(
        self, stream_id: str, uid: str, tid: str, cfg: Any, *, locked: bool = False
    ) -> str:
        """起卦后：解读 → 发送 → 结算。失败保留状态，下次接着完成。"""
        guard = self._guard_store()
        state = guard.load()
        rec = user_rec(state, uid)
        topic = (rec.get("topics") or {}).get(tid)
        if not isinstance(topic, dict) or topic.get("status") != "casting":
            return ""

        sign = topic.get("sign") or {}
        entry = sign.get("entry") or {}
        roll_info = sign.get("roll") or {}
        line = str(topic.get("line") or "")
        digest = str(topic.get("digest") or "")

        if not digest:
            digest = await self._digest(entry, roll_info, str(topic.get("text") or ""), cfg)
            async with (guard.lock() if not locked else _NULL_LOCK):
                state = guard.load()
                rec = user_rec(state, uid)
                t = (rec.get("topics") or {}).get(tid) or {}
                if isinstance(t, dict) and t.get("status") == "casting":
                    t["digest"] = digest
                    t["last_seen"] = time.time()
                guard.save(state)

        if not bool(topic.get("sent_line")):
            ok, sign_msg_id = await self._say_ok(line, stream_id)
            async with (guard.lock() if not locked else _NULL_LOCK):
                state = guard.load()
                rec = user_rec(state, uid)
                t = (rec.get("topics") or {}).get(tid) or {}
                if isinstance(t, dict):
                    t["sent_line"] = ok
                    t["last_seen"] = time.time()
                    if ok and sign_msg_id:
                        t["sign_msg_id"] = sign_msg_id
                guard.save(state)
            if not ok:
                return line

        if not bool(topic.get("sent_digest")):
            ok2, _ = await self._say_ok(digest, stream_id)
            async with (guard.lock() if not locked else _NULL_LOCK):
                state = guard.load()
                rec = user_rec(state, uid)
                t = (rec.get("topics") or {}).get(tid) or {}
                if isinstance(t, dict):
                    t["sent_digest"] = ok2
                    t["last_seen"] = time.time()
                    if ok2 and t.get("status") == "casting":
                        now = time.time()
                        t["status"] = "answered"
                        t["answer"] = f"{line}\n{digest}"
                        t["answered_at"] = now
                        t["strikes"] = 0
                        rec["successes"] = [
                            float(x) for x in (rec.get("successes") or []) if now - float(x) < HOUR_SECONDS
                        ]
                        rec["successes"].append(now)
                        rec["day_success"] = int(rec.get("day_success") or 0) + 1
                        rec["last_success"] = now
                guard.save(state)

        return f"{line}\n{digest}"

    def _log_throw(
        self,
        stream_id: str,
        question: str,
        roll_info: dict[str, Any],
        throw_entry: dict[str, Any],
    ) -> None:
        try:
            path = self.ctx.paths.data_dir / "astragaloi.jsonl"
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stream_id": stream_id,
                "q": question,
                "raw_bones": roll_info["raw_bones"],
                "key": roll_info["key"],
                "sum": roll_info["sum"],
                "index": throw_entry.get("index"),
                "deity_zh": throw_entry.get("deity_zh"),
                "deity_grc": throw_entry.get("deity_grc"),
                "verdict": throw_entry.get("verdict"),
                "prob_real": throw_entry.get("prob_real"),
                "source_ref": throw_entry.get("source_ref"),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    async def _digest(
        self,
        throw_entry: dict[str, Any],
        roll_info: dict[str, Any],
        question: str,
        cfg: Any,
    ) -> str:
        oracle_zh = str(throw_entry.get("oracle_zh") or "").strip()
        imagery_zh = str(throw_entry.get("imagery_zh") or "").strip()
        verdict = str(throw_entry.get("verdict") or "").strip()
        deity_zh = str(throw_entry.get("deity_zh") or "").strip()
        fallback = f"石柱第 {throw_entry.get('index', '?')} 签归「{deity_zh}」（{verdict}）：{oracle_zh}"

        if not cfg or not getattr(cfg, "digest", True):
            return fallback

        lo = int(getattr(cfg, "min_chars", 80) or 80)
        hi = int(getattr(cfg, "max_chars", 150) or 150)
        raw_faces_desc = "、".join(FACE_SHORT_ZH.get(b, str(b)) for b in roll_info["raw_bones"])
        deity_ctx = self._deity_context_line(throw_entry)
        prob_pct = float(throw_entry.get("prob_real") or 0.0) * 100.0
        q_line = f"老大问的事：{question}\n" if question else "老大没指明具体哪桩，就按眼下心里正盘算要办的事来读。\n"

        prompt = (
            q_line
            + "你是呆猫，本名小苗。外表憨憨，心思直接，叫对方“老大”。\n"
            "你刚替老大在市集神谕石柱前掷出了五颗小亚细亚羊距骨，正蹲在石柱旁把刻着的古希腊神签翻成大白话给老大听。\n\n"
            f"【落骨实况】五颗骨头依次落出：{raw_faces_desc} → 归入骨相 {roll_info['key']}（总点数 {roll_info['sum']}，物理出现率 {prob_pct:.3f}%）。\n"
            f"【冠名神签】第 {throw_entry.get('index')} 签 · {deity_zh}（{throw_entry.get('deity_grc', '')}）｜判目：{verdict}\n"
            + (f"【神祇背景参考（仅供拿准分寸，严禁背百科词条）】{deity_ctx}\n" if deity_ctx else "")
            + f"【碑铭核心比兴】{imagery_zh}\n"
            + f"【原碑六音步诗忠实译文】{oracle_zh}\n\n"
            "怎么说给老大听（硬规则）：\n"
            "1. 对神明保持敬畏：不轻慢、不拿神明开玩笑、不编排神明的私事和脾气，也绝对不要假装自己是那位神明在降神说话——你只是帮老大看石柱刻字的呆猫。\n"
            "2. 保留古碑里最鲜活的那个比兴画面（比如往海里撒种、群蝎拦路、右翼神鹰、无花果熟了、解缆起航、盲眼小狗等），用它把老大问的这件事是“该放手干、该立刻停手、还是得熬过眼下等时机”一口咬死，别说模棱两可的现代鸡汤。\n"
            f"3. 口语、短句，一段话直接说完，不换行、不分点、不用 markdown，字数控制在 {lo}~{hi} 字左右。\n"
            "只输出这段话本身。"
        )
        try:
            result = await self.ctx.call_capability(
                "llm.generate",
                timeout_ms=90000,
                prompt=prompt,
                task_name=str(getattr(cfg, "digest_task", "replyer") or "replyer"),
                model_name=str(getattr(cfg, "digest_model", "") or ""),
                temperature=0.75,
                max_tokens=int(getattr(cfg, "digest_max_tokens", 800) or 800),
            )
            resp = str((result or {}).get("response") or "").strip()
            if resp:
                return resp
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("五羊距骨碑铭消化失败，退回原碑直译: %s", exc)
        return fallback


def create_plugin() -> AstragaloiPlugin:
    return AstragaloiPlugin()
