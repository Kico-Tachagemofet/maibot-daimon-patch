"""呆猫底色与门控 v0.4.0：单脉搏底色（22大阿卡纳+星骰）+ 严格传统二进制茭杯门控 + 出口封口。

- Planner 每轮先调 daimon_pulse 定唯一底色（22张大阿卡纳正/逆 + 星骰），消除与 tarot_divine 的双抽抢拍；
- 求占、支使、索取或逗猫试探时，daimon_pulse 真掷两枚二进制位（1=阳，0=阴）：
  1/0 或 0/1 为圣杯（50%），1/1 为笑杯（25%），0/0 为阴杯（25%）；
- 笑杯与阴杯的具体缘由以当拍塔罗与星骰为准，结合聊天语境理解（状态列表仅作参考）；
- Replyer 阶段直接复用本拍已抽的同一套牌+骰+杯，绝不二次重抽抢主线；
- 正文出现自定义过滤词时整句替换为固定台词。
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from maibot_sdk import (
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

MAIBOT_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path.cwd()).expanduser().resolve()
RESOURCE_ROOT = MAIBOT_ROOT / "data" / "daimon-resources"


def _configured_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else MAIBOT_ROOT / path


PULSE_STATE_PATH = MAIBOT_ROOT / "temp" / "daimon_pulse_state.json"
PULSE_TTL_SECONDS = 120.0

DEFAULT_TABLE: Dict[str, List[List[str]]] = {
    "planets": [
        ["太阳", "想被看见，有点端"],
        ["月亮", "心里软，想照顾人"],
        ["水星", "想搭话，嘴快"],
        ["金星", "想讨人喜欢，黏"],
        ["火星", "有点上火，想顶回去"],
        ["木星", "心大，想吹两句"],
        ["土星", "收着，不太想给反应"],
        ["天王", "走神，想岔开"],
        ["海王", "犯迷糊，懒懒的"],
        ["冥王", "盯住了，不想放过"],
    ],
    "signs": [
        ["白羊", "直冲，不拐弯"],
        ["金牛", "慢，拖着说"],
        ["双子", "岔来岔去"],
        ["巨蟹", "糯，绕回自己人"],
        ["狮子", "端着，要面子"],
        ["处女", "挑细节"],
        ["天秤", "客气，两边都顾"],
        ["天蝎", "藏着，说一半"],
        ["射手", "跑题"],
        ["摩羯", "硬，少字"],
        ["水瓶", "怪，跳着说"],
        ["双鱼", "飘，含糊"],
    ],
    "houses": [
        ["1宫", "自己身上"],
        ["2宫", "值不值、有没有用"],
        ["3宫", "话头上"],
        ["4宫", "家里、熟的地方"],
        ["5宫", "玩和逗上"],
        ["6宫", "日常琐事上"],
        ["7宫", "对面那个人身上"],
        ["8宫", "深一点的地方"],
        ["9宫", "远处、别处"],
        ["10宫", "台面上、正经场合"],
        ["11宫", "一群人里"],
        ["12宫", "说不清的地方"],
    ],
}

# 保留配置字段兼容；印象与运势解读统一走 tarot_divine，不再在 before_request 二次抽牌抢主线
DEFAULT_READING_KEYWORDS = ["印象", "怎么看", "看法", "感觉如何"]

# 触发茭杯门控的典型求占/讨要/逗猫试探词（兜底辅助 Planner 的 gate 参数，避免泛化日常词）
GATE_HINT_WORDS = (
    "运势", "走向", "算一下", "算一卦", "起一卦", "起卦", "断一卦", "断一下",
    "抽牌", "抽一张", "印象", "怎么看", "看法", "感觉如何", "成不成", "能不能成",
    "什么时候有结果", "应期", "往哪个方向", "丢哪", "找不着", "吃啥", "吃什么",
    "选哪个", "哪个好", "摸摸", "抱抱", "亲亲", "叫一声", "喵一声", "距骨", "骨签",
)


def _extract_text(data: Any) -> str:
    """从消息结构里尽量挖出正文；挖不到返回空串。"""
    if isinstance(data, dict):
        for key in ("processed_plain_text", "plain_text", "text", "raw_text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _extract_text(value)
            if found:
                return found
    elif isinstance(data, (list, tuple)):
        for value in data:
            found = _extract_text(value)
            if found:
                return found
    elif hasattr(data, "model_dump"):
        try:
            return _extract_text(data.model_dump())
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _read_shared_state() -> Dict[str, Any]:
    try:
        if PULSE_STATE_PATH.exists():
            data = json.loads(PULSE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {"active_session_id": "", "active_ts": 0.0, "sessions": {}}


def _write_shared_state(state: Dict[str, Any]) -> None:
    try:
        PULSE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        sessions = state.get("sessions")
        if isinstance(sessions, dict):
            expired = [k for k, v in sessions.items() if not isinstance(v, dict) or now - float(v.get("ts", 0)) > 600]
            for k in expired:
                sessions.pop(k, None)
        tmp_path = PULSE_STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, PULSE_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def roll_binary_jiaobei(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """严格传统二进制茭杯：roll 两个 2 进制位，1 为阳、0 为阴。
    - 1/0 或 0/1 -> 圣杯（50%）
    - 1/1 -> 笑杯（25%）
    - 0/0 -> 阴杯（25%）
    """
    r = rng or random.SystemRandom()
    b1 = r.randint(0, 1)
    b2 = r.randint(0, 1)
    if b1 != b2:
        cup = "圣杯"
    elif b1 == 1:
        cup = "笑杯"
    else:
        cup = "阴杯"
    return {
        "bits": [b1, b2],
        "bits_str": f"{b1}/{b2}",
        "yang_count": b1 + b2,
        "cup": cup,
    }


def _jiaobei_guidance(jiaobei: Dict[str, Any], tarot: Optional[Dict[str, Any]], seg_label: str, seg_attitude: str) -> str:
    cup = jiaobei["cup"]
    bits_str = jiaobei["bits_str"]
    tarot_line = ""
    if tarot:
        tarot_line = (
            f"当拍塔罗：{tarot['title']}（{tarot['orientation']}）｜牌面气口：{tarot.get('short', '')}\n"
        )
    if cup == "圣杯":
        return (
            f"【茭杯门控：{bits_str} 圣杯（一阳一阴 · 肯接）】\n"
            f"{tarot_line}"
            f"当拍星骰：{seg_label}（{seg_attitude}）\n"
            "通道敞开，这一拍肯接。"
            "若只是日常逗猫/顺口要求，直接调 reply 按牌骰底色回话（不用报牌名或杯名）；"
            "若问运势/走向/印象，接着调 tarot_divine；"
            "若问具体事（成败、应期、寻物、方位、选择、吃什么），接着调 meihua_divine（形成塔罗+星骰与梅花二则合参）。"
        )
    if cup == "笑杯":
        return (
            f"【茭杯门控：{bits_str} 笑杯（两阳 · 歪头晃尾巴，不给死准话）】\n"
            f"{tarot_line}"
            f"当拍星骰：{seg_label}（{seg_attitude}）\n"
            "硬规则：这一拍出笑杯，不要再调用 tarot_divine 或 meihua_divine，直接调 reply 回应！\n"
            "怎么拿准笑杯的意思：**以这张塔罗牌和星骰的劲头为主依据**，结合当前聊天语境去理解为什么出笑杯。\n"
            "状态参考（仅作参考，不硬套，按语境与牌骰落点）：可能是问话的人对事情本身的理解就偏了或前提好笑、"
            "对方心里明明早有主意还来套话找垫背、问题缠成毛线团没问清、局势还在半空晃猫先揣爪看戏、"
            "或识破假鱼干画饼故意逗对方玩。顺着牌和骰子的气口用呆猫短句说一两句，不报牌名和“笑杯”术语（除非对方专门问摔杯结果）。"
        )
    return (
        f"【茭杯门控：{bits_str} 阴杯（两阴 · 扣爪合上通道，这拍不接）】\n"
        f"{tarot_line}"
        f"当拍星骰：{seg_label}（{seg_attitude}）\n"
        "硬规则：这一拍出阴杯，通道落锁，禁止再调用 tarot_divine 或 meihua_divine，直接调 reply 挡回或点破！\n"
        "怎么拿准阴杯的意思：**以这张塔罗牌和星骰的劲头为主依据**，结合当前聊天语境去理解为什么不接。\n"
        "状态参考（仅作参考，不硬套，按语境与牌骰落点）：可能是**问话的人对事情本身的理解就完全有问题、前提就错了没法顺着算**，"
        "也可能是现在猫不想动爪、现在猫在忙别的事、猫脑过载了喵、问法钻牛角尖路子不对、或这会儿气闷拍不开。"
        "若牌骰与语境指向对方前提想岔了，就直球点破他哪儿想偏了；若指向猫自身状态，就按呆猫脾气干脆推掉；不报牌名和“阴杯”术语（除非对方专门问摔杯结果）。"
    )


@dataclass
class Segment:
    label: str
    attitude: str
    started_at: float
    last_at: float
    parts: Dict[str, str] = field(default_factory=dict)


class PulseSection(PluginConfigBase):
    config_version: str = Field(default="1.5.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用态度层")
    prayer: str = Field(
        default="让这一拍的牌骰成为呆猫此刻的底色。",
        description="每次注入的底色引导句，可自行改写",
    )
    headache_line: str = Field(
        default="那是什么，头好痛，要长脑子了。",
        description="正文命中过滤词时的固定台词",
    )
    taboo_words: List[str] = Field(
        default_factory=list,
        description="出口过滤词；正文命中即整句替换",
    )
    segment_minutes: int = Field(
        default=15, description="一段对话最长多少分钟换一次态度（话题由模型判断）"
    )
    gap_minutes: int = Field(
        default=5, description="静默超过多少分钟算新的一段"
    )
    protected_names: List[str] = Field(
        default_factory=list,
        description="禁区名字：问到这些人时不掷骰、不评价，只回一句挡掉",
    )
    protected_reply: str = Field(
        default="这个我不看。",
        description="命中禁区时唯一的回复",
    )
    per_reply_roll: bool = Field(
        default=True,
        description="每一条回复都重掷一次，给这一轮定调（由 daimon_pulse 统一产出并复用）",
    )
    reading_keywords: List[str] = Field(
        default_factory=lambda: list(DEFAULT_READING_KEYWORDS),
        description="保留兼容字段；印象与运势统一交由 tarot_divine 处理",
    )
    tarot_first: bool = Field(
        default=True,
        description="每拍先抽一张22大阿卡纳与星骰共同定底色",
    )
    jiaobei_enabled: bool = Field(
        default=True,
        description="启用严格传统二进制茭杯门控（1=阳/0=阴，50%圣/25%笑/25%阴）",
    )
    serious_group_ids: List[str] = Field(
        default_factory=list,
        description="严肃学术群 ID 或 stream_id（免检日常逗猫拒答杯，严守群规与敬神）",
    )
    tarot_pack_dir: str = Field(
        default=str(RESOURCE_ROOT / "tarot-meanings"),
        description="塔罗牌意库目录（含 card_meanings.py）",
    )


class PulseConfig(PluginConfigBase):
    plugin: PulseSection = Field(default_factory=PulseSection)


class DaimonPulsePlugin(MaiBotPlugin):
    config_model = PulseConfig

    def __init__(self) -> None:
        super().__init__()
        self._segments: Dict[str, Segment] = {}
        self._table: Dict[str, List[List[str]]] = DEFAULT_TABLE
        self._table_path: Optional[Path] = None
        self._table_loaded_at: float = 0.0
        self._text_cache: Dict[str, str] = {}
        self._tarot_cards: Optional[Dict[str, Any]] = None
        self._tarot_meta: Dict[str, Any] = {}
        self._last_planner_sid: str = ""

    # ── 生命周期 ────────────────────────────────────────────
    async def on_load(self) -> None:
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._table_path = data_dir / "attitudes.json"
        if not self._table_path.exists():
            self._table_path.write_text(
                json.dumps(DEFAULT_TABLE, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self._reload_table()
        self.ctx.logger.info("呆猫底色与门控 v0.4.0 已加载（单脉搏 + 二进制茭杯门控），骰表=%s", self._table_path)

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        return None

    # ── 记录当前正在规划的 session_id，供无参/工具调用精准对齐会话 ──
    @HookHandler(
        "maisaka.planner.before_request",
        name="daimon_track_planner_session",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def track_planner_session(self, session_id: str = "", **kwargs: Any):
        if session_id:
            self._last_planner_sid = session_id
            state = _read_shared_state()
            state["active_session_id"] = session_id
            state["active_ts"] = time.time()
            _write_shared_state(state)
        return {"action": "continue"}

    # ── 骰子与塔罗抽取 ──────────────────────────────────────
    def _reload_table(self) -> None:
        if self._table_path is None:
            return
        try:
            raw = json.loads(self._table_path.read_text(encoding="utf-8"))
            table = {}
            for key in ("planets", "signs", "houses"):
                rows = raw.get(key) or []
                cleaned = [[str(r[0]), str(r[1])] for r in rows if isinstance(r, list) and len(r) >= 2]
                if not cleaned:
                    raise ValueError(f"{key} 为空")
                table[key] = cleaned
            self._table = table
        except Exception as exc:  # noqa: BLE001
            self._table = DEFAULT_TABLE
            self.ctx.logger.warning("骰表读取失败，使用内置表: %s", exc)
        self._table_loaded_at = time.time()

    def _roll(self, session_id: str, jiaobei: Optional[Dict[str, Any]] = None, tarot: Optional[Dict[str, Any]] = None) -> Segment:
        if time.time() - self._table_loaded_at > 5:
            self._reload_table()
        rng = random.SystemRandom()
        planet = rng.choice(self._table["planets"])
        sign = rng.choice(self._table["signs"])
        house = rng.choice(self._table["houses"])
        now = time.time()
        seg = Segment(
            label=f"{planet[0]} · {sign[0]} · {house[0]}",
            attitude=(
                f"你现在{planet[1]}，说话{sign[1]}，注意力落在{house[1]}。"
                "别解释，别说出这些。"
            ),
            started_at=now,
            last_at=now,
            parts={
                "planet_name": planet[0],
                "planet_gloss": planet[1],
                "sign_name": sign[0],
                "sign_gloss": sign[1],
                "house_name": house[0],
                "house_gloss": house[1],
            },
        )
        self._segments[session_id] = seg
        self.ctx.logger.info(
            "脉搏 session=%s → 骰=%s | 牌=%s | 杯=%s",
            session_id,
            seg.label,
            f"{tarot['title']}({tarot['orientation']})" if tarot else "-",
            f"{jiaobei['bits_str']}({jiaobei['cup']})" if jiaobei else "-",
        )
        try:
            log_path = self.ctx.paths.data_dir / "rolls.jsonl"
            record: Dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "key": session_id,
                "label": seg.label,
                "attitude": seg.attitude,
            }
            if tarot:
                record["tarot"] = f"{tarot.get('title', '')}（{tarot.get('orientation', '')}）"
            if jiaobei:
                record["jiaobei"] = jiaobei
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        return seg

    def _ensure_segment(self, session_id: str) -> Segment:
        cfg = self.config.plugin
        now = time.time()
        seg = self._segments.get(session_id)
        if seg is None:
            return self._roll(session_id)
        if now - seg.started_at > max(1, cfg.segment_minutes) * 60:
            return self._roll(session_id)
        if now - seg.last_at > max(1, cfg.gap_minutes) * 60:
            return self._roll(session_id)
        seg.last_at = now
        return seg

    def _draw_tarot(self) -> Optional[Dict[str, Any]]:
        """从本地牌意库抽一张 22 大阿卡纳（含正/逆位）；失败时返回 None。"""
        cfg = self.config.plugin
        if not getattr(cfg, "tarot_first", True):
            return None
        try:
            if self._tarot_cards is None:
                pack_dir = _configured_path(getattr(cfg, "tarot_pack_dir", "") or RESOURCE_ROOT / "tarot-meanings")
                module_path = pack_dir / "card_meanings.py"
                spec = importlib.util.spec_from_file_location("daimon_tarot_pack", module_path)
                if spec is None or spec.loader is None:
                    raise ImportError("无法加载塔罗牌意库")
                module = importlib.util.module_from_spec(spec)
                import sys
                sys.modules["daimon_tarot_pack"] = module
                spec.loader.exec_module(module)
                pack = module.MeaningPack(pack_dir)
                self._tarot_cards = pack.select(list(module.MAJOR_IDS))["cards"]
                self._tarot_meta = dict(pack.metadata)
            rng = random.SystemRandom()
            card_id, card = rng.choice(list(self._tarot_cards.items()))
            text = re.sub(r"^#[^\n]*\n+", "", str(card.get("text") or ""), count=1)
            clean = re.sub(r"\s+", "", text)
            short = clean[:160]
            cut = short.rfind("。")
            if cut >= 40:
                short = short[: cut + 1]
            return {
                "id": card_id,
                "title": str(card.get("title") or ""),
                "orientation": rng.choice(["正位", "逆位"]),
                "short": short,
            }
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("塔罗牌意库不可用，退回骰子: %s", exc)
            return None

    def _resolve_session_id(self, kwargs: Dict[str, Any]) -> str:
        explicit = str(
            kwargs.get("session_id")
            or kwargs.get("stream_id")
            or kwargs.get("chat_id")
            or self._last_planner_sid
            or ""
        ).strip()
        return explicit or "default"

    # ── 目标消息正文 ────────────────────────────────────────
    async def _target_text(self, message_id: str, session_id: str) -> str:
        if not message_id:
            return ""
        cached = self._text_cache.get(message_id)
        if cached is not None:
            return cached
        text = ""
        try:
            data = await self.ctx.message.get_by_id(message_id, stream_id=session_id or "")
            text = _extract_text(data)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("取目标消息失败: %s", exc)
        if len(self._text_cache) > 256:
            self._text_cache.clear()
        self._text_cache[message_id] = text
        return text

    # ── 核心工具：唯一底色脉搏 + 二进制茭杯门控 ──────────────
    @Tool(
        "daimon_pulse",
        brief_description="开口前必调的灵体摸脉：一次抽好本拍大阿卡纳+星骰底色，并在求占/索取/试探时掷二进制茭杯",
        detailed_description=(
            "任何回复前都先调用本工具（毫秒级本地抽取，不耗模型），让这一拍有牌与骰说话：\n"
            "- 闲聊、打趣、普通顺嘴接话：传 gate=false，只定塔罗+星骰底色，随后直接调 reply（牌名不用说出口）。\n"
            "- 问到某个神/神祇：先调 deity_lore（本地无卡先 web_search），再调本工具并传 gate=false, deity_topic=true（敬神免摔拒答杯）。\n"
            "- 对方向呆猫求占、问印象/运势、问具体事（成败/应期/寻物/方位/选择/吃啥）、讨摸支使、发假鱼干逗猫或追问“好不好/行不行/对不对”：传 gate=true 掷二进制茭杯（1=阳，0=阴：圣杯50%/笑杯25%/阴杯25%）。\n"
            "  * 若出【圣杯】：问运势/印象接着调 tarot_divine；问具体事接着调 meihua_divine（形成塔罗+星骰与梅花的二则合参）；日常逗猫要求直接调 reply。\n"
            "  * 若出【笑杯】或【阴杯】：绝对不要再调 tarot_divine 或 meihua_divine！以本工具返回的塔罗与星骰为主依据、结合聊天语境理解原因（如对方对事情理解完全有问题、或猫不想/在忙/猫脑过载等），直接调 reply 回应。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="对方刚说的原话或当前要回应的话头",
                required=True,
            ),
            ToolParameterInfo(
                name="gate",
                param_type=ToolParamType.BOOLEAN,
                description="是否需要过茭杯门控（求占/看印象/问成败吃啥/讨要支使/逗猫下套为 true；普通接闲话与严肃神祇话题为 false）",
                required=False,
                default=False,
            ),
            ToolParameterInfo(
                name="deity_topic",
                param_type=ToolParamType.BOOLEAN,
                description="是否属于已查过 deity_lore 的严肃神明/神祇话题（仅真实神祇话题传 true 免摔拒答杯，其余一律 false）",
                required=False,
                default=False,
            ),
        ],
        visibility="visible",
    )
    async def daimon_pulse_tool(
        self,
        question: str = "",
        gate: bool = False,
        deity_topic: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        cfg = self.config.plugin
        sid = self._resolve_session_id(kwargs)
        q_text = str(question or "").strip()

        # 命中保护名单时直接给禁区挡回指令
        protected_hit = next(
            (name for name in cfg.protected_names if name and name.casefold() in q_text.casefold()),
            "",
        )
        if protected_hit:
            return {
                "success": True,
                "message": (
                    f"【禁区名字】不要猜、不要描述、不要提任何名字，直接调 reply 只回一句：「{cfg.protected_reply}」并岔开话题。"
                ),
            }

        state = _read_shared_state()
        sessions = state.setdefault("sessions", {})
        now = time.time()

        # 防止同一轮思考里重复连调 daimon_pulse 覆盖当拍符号（12秒内未消费的同话头直接复用）
        existing = sessions.get(sid)
        if (
            isinstance(existing, dict)
            and not existing.get("reply_consumed")
            and now - float(existing.get("ts", 0)) < 12.0
            and str(existing.get("question") or "") == q_text
        ):
            return {"success": True, "message": str(existing.get("pulse_message") or "本拍底色已就绪。")}

        tarot = self._draw_tarot()
        is_deity = bool(deity_topic)
        should_gate = bool(gate) and getattr(cfg, "jiaobei_enabled", True) and not is_deity
        if not should_gate and getattr(cfg, "jiaobei_enabled", True) and not is_deity:
            if any(w in q_text for w in GATE_HINT_WORDS):
                # 严肃群的非占卜讨论不轻易自动升格为 gate
                is_serious = any(g and g in sid for g in getattr(cfg, "serious_group_ids", []))
                if not is_serious or any(w in q_text for w in ("起卦", "算一", "断一", "运势", "印象", "距骨")):
                    should_gate = True

        jiaobei = roll_binary_jiaobei() if should_gate else None
        seg = self._roll(sid, jiaobei=jiaobei, tarot=tarot)

        if jiaobei:
            msg = _jiaobei_guidance(jiaobei, tarot, seg.label, seg.attitude)
        else:
            tarot_part = (
                f"牌：{tarot['title']}（{tarot['orientation']}）｜气口：{tarot.get('short', '')[:90]}\n"
                if tarot
                else ""
            )
            msg = (
                "【本拍底色已定 · 闲聊不外显】\n"
                f"{tarot_part}"
                f"骰：{seg.label}（{seg.attitude}）\n"
                "把这张牌的气质和骰子的劲头一起当这一句的底色，直接调 reply 说话；不要报牌名、不要提“牌/卦/骰”。"
            )

        sessions[sid] = {
            "ts": now,
            "question": q_text,
            "is_deity": is_deity,
            "tarot": tarot,
            "dice": {
                "label": seg.label,
                "attitude": seg.attitude,
                **seg.parts,
            },
            "jiaobei": jiaobei,
            "divine_mode": "",
            "reply_consumed": False,
            "consumed_by_msg_id": "",
            "pulse_message": msg,
        }
        state["active_session_id"] = sid
        state["active_ts"] = now
        _write_shared_state(state)
        return {"success": True, "message": msg}

    # ── Replyer 注入（严格复用本拍脉搏，消除二次重抽）────────
    @HookHandler(
        "maisaka.replyer.before_request",
        name="daimon_attitude_inject",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_attitude(
        self,
        extra_prompt: str = "",
        session_id: str = "",
        reply_message_id: str = "",
        **kwargs: Any,
    ):
        cfg = self.config.plugin
        if not cfg.enabled:
            return {"action": "continue"}
        key = session_id or "default"

        target_text = await self._target_text(reply_message_id, session_id)
        protected_hit = next(
            (name for name in cfg.protected_names if name and name.casefold() in target_text.casefold()),
            "",
        )
        if protected_hit:
            merged = dict(kwargs)
            merged["extra_prompt"] = (extra_prompt or "") + (
                "\n\n【这个问题不接】\n"
                "对方问的是一个不该由你看、也不该由你评价的名字。\n"
                "不要掷骰子，不要猜，不要描述他，不要提任何名字。\n"
                f"只回一句：{cfg.protected_reply} 然后把话题岔到别的地方。\n"
            )
            return {"modified_kwargs": merged}

        # 严格按当前 session_id 读取本拍共享脉搏（禁止跨会话扫描，杜绝多群串线）
        now = time.time()
        state = _read_shared_state()
        sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        pulse: Optional[Dict[str, Any]] = None

        cand = sessions.get(key)
        same_msg_rebubble = bool(
            reply_message_id
            and isinstance(cand, dict)
            and cand.get("consumed_by_msg_id") == reply_message_id
        )
        if (
            isinstance(cand, dict)
            and (not cand.get("reply_consumed") or same_msg_rebubble)
            and now - float(cand.get("ts", 0)) < PULSE_TTL_SECONDS
        ):
            pulse = cand

        turn_tarot: Optional[Dict[str, Any]] = None
        jiaobei: Optional[Dict[str, Any]] = None
        divine_mode = ""

        if pulse is not None:
            dice_info = pulse.get("dice") or {}
            seg_label = str(dice_info.get("label") or "")
            seg_attitude = str(dice_info.get("attitude") or "")
            turn_tarot = pulse.get("tarot") if isinstance(pulse.get("tarot"), dict) else None
            jiaobei = pulse.get("jiaobei") if isinstance(pulse.get("jiaobei"), dict) else None
            divine_mode = str(pulse.get("divine_mode") or "")
            pulse["reply_consumed"] = True
            if reply_message_id:
                pulse["consumed_by_msg_id"] = reply_message_id
            _write_shared_state(state)
        else:
            # 兜底：若 Planner 漏调 daimon_pulse，才在本地补抽一次，保证任何开口都有符号承载
            if getattr(cfg, "per_reply_roll", True):
                turn_tarot = self._draw_tarot()
                seg = self._roll(key, tarot=turn_tarot)
            else:
                seg = self._ensure_segment(key)
            seg_label = seg.label
            seg_attitude = seg.attitude

        block = (
            "\n\n【此刻的态度 · 不外显】\n"
            f"{cfg.prayer}\n"
            f"{seg_attitude}\n"
            "它只决定你这一句的底色。不要解释它，不要说出来，不要提任何人名。\n"
            "但这一句的用词和节奏要能看出这个底色（懒、急、软、冲），不要写成没有情绪的中性句。\n"
        )

        # 若本轮出现笑杯或阴杯，注入以塔罗+星骰+语境为准的裁断要求
        if jiaobei and jiaobei.get("cup") in ("笑杯", "阴杯"):
            block += "\n" + _jiaobei_guidance(jiaobei, turn_tarot, seg_label, seg_attitude) + "\n"
        elif turn_tarot and not divine_mode:
            # 仅当本轮未走 tarot_divine / meihua_divine / astragaloi 正式解读时，才注入隐式底色牌，彻底杜绝双牌抢主线
            block += (
                "\n【这一拍的牌 + 骰（整包，不外显）】\n"
                f"牌：{turn_tarot['title']}（{turn_tarot['orientation']}）｜骰：{seg_label}\n"
                f"牌面提示：{turn_tarot.get('short', '')[:90]}\n"
                "用法：把这张牌的气质和骰子的劲头一起当这一句的底色，只影响语气和取景，"
                "不要解牌、不要报牌名、不要提“牌”“卦”“骰”这些字。\n"
            )

        relay = any(word in target_text for word in ("帮我问", "替我问问", "帮我问问", "去问", "替我打听"))
        third_state = any(word in target_text for word in ("在干嘛", "想干啥", "想干什么", "干嘛呢", "在做什么")) and any(
            word in target_text for word in ("他", "她", "ta", "TA", "人家")
        )
        if relay or third_state:
            block += (
                "\n【关于打听别人】\n"
                "- 你联系不到第三方，不要假装去问过了，也不要替对方回答。\n"
                "- 不要编对方在做什么、想什么、会怎么做；不要描述对方的身份或私事。\n"
                "- 只能给倾向：说清这是你猜的，或者直接说“我又联系不上他”。\n"
                "- 如果手里已经有占卜结果（且茭杯为圣杯放行），先正面回答对方问的那件事（例如“八成是想着……”），再补一句为什么；不要只给气质描述。\n"
            )

        merged = dict(kwargs)
        merged["extra_prompt"] = (extra_prompt or "") + block
        return {"modified_kwargs": merged}

    # ── 出口过滤 / 保留牌意署名 ──────────────────────────────
    @HookHandler(
        "maisaka.reply.before_post_process",
        name="daimon_taboo_scrub",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def scrub(
        self,
        response: str = "",
        reply_message_id: str = "",
        **kwargs: Any,
    ):
        cfg = self.config.plugin
        raw_resp = response or ""
        # 保护末尾“— 牌意：”署名行不被拼进正文
        protected = raw_resp.replace("\r\n", "\n").replace("\r", "\n")
        protected = protected.replace("\n— 牌意：", "\ue000— 牌意：")
        text = protected.replace("\n", "").replace("\ue000", "\n")
        low = text.casefold()
        for word in cfg.taboo_words:
            if word and word.casefold() in low:
                self.ctx.logger.info("出口命中过滤词 %s，替换为固定台词", word)
                merged = dict(kwargs)
                merged["reply_message_id"] = reply_message_id
                merged["response"] = cfg.headache_line
                return {"modified_kwargs": merged}
        merged = dict(kwargs)
        merged["reply_message_id"] = reply_message_id
        merged["response"] = text
        if "— 牌意：" in text:
            merged["enable_splitter"] = False
        return {"modified_kwargs": merged}

    # ── 发送前最后一道：抹掉多余换行 ─────────────────────────
    @HookHandler(
        "send_service.before_send",
        name="daimon_tidy_outbound",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def tidy_outbound(self, message=None, **kwargs: Any):
        if not isinstance(message, dict):
            return {"action": "continue"}
        raw = message.get("raw_message")
        changed = False
        if isinstance(raw, list):
            for seg in raw:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    data = seg.get("data")
                    if isinstance(data, str) and ("\n" in data or "\r" in data):
                        protected = data.replace("\n—", "\ue000—").replace("\r—", "\ue000—")
                        protected = protected.replace("\n【", "\ue001【").replace("\r【", "\ue001【")
                        protected = protected.replace("\r", "").replace("\n", "")
                        seg["data"] = protected.replace("\ue000", "\n").replace("\ue001", "\n")
                        changed = True
        if not changed:
            return {"action": "continue"}
        merged = dict(kwargs)
        merged["message"] = message
        return {"modified_kwargs": merged}

    # ── 工具：话题换了手动重掷 ───────────────────────────────
    @Tool(
        "daimon_redraw",
        description=(
            "当聊天的话题明显切换到了另一件事（不是顺着上一句接），调用它重掷这一段对话的底色。"
            "只在话题真的换了的时候调用，不要每轮都调用；同一话题内不要重复调用。"
        ),
    )
    async def redraw(self, **kwargs: Any):
        sid = self._resolve_session_id(kwargs)
        tarot = self._draw_tarot()
        seg = self._roll(sid, tarot=tarot)
        return {"success": True, "message": f"已换成新的底色（{seg.label}），不必说出来。"}

    # ── 命令 ────────────────────────────────────────────────
    @Command("daimon_roll", description="重掷一次态度骰", pattern=r"^/掷骰\s*$")
    async def handle_roll(self, **kwargs: Any):
        sid = str(kwargs.get("session_id") or kwargs.get("stream_id") or "default")
        seg = self._roll(sid)
        return True, f"掷骰：{seg.label}\n{seg.attitude}", 2

    @Command("daimon_jiaobei", description="/摔杯 查看一次二进制茭杯与当拍牌骰", pattern=r"^\s*/(?:摔杯|茭杯|筊杯)\s*$")
    async def handle_jiaobei(self, **kwargs: Any):
        sid = str(kwargs.get("session_id") or kwargs.get("stream_id") or "default")
        tarot = self._draw_tarot()
        jb = roll_binary_jiaobei()
        seg = self._roll(sid, jiaobei=jb, tarot=tarot)
        t_str = f"｜{tarot['title']}（{tarot['orientation']}）" if tarot else ""
        return True, f"茭杯：{jb['bits_str']} → {jb['cup']}（1阳0阴）{t_str}｜{seg.label}", 2


def create_plugin():
    return DaimonPulsePlugin()
