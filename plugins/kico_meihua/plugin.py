"""梅花易数 · 一卦多断（MaiBot 插件 v2.1 · 塔罗+星骰与梅花二则合参协同版）

起卦沿用 Kico 的 meihua-yishu 规则：三个 1-99 随机数 → 上卦/下卦/动爻。
在起卦结果上补：互卦、之卦、体用、五行生克、月令旺衰、应期数、八卦取象，
并与本拍第一则（daimon_pulse 抽出的 22 大阿卡纳+星骰底色）合参，按题型断成一段话。

两条入口（按 Kico 定的触发规则）：
  1. 显式 —— 聊天里出现「梅花 / 起卦 / 断一卦 / 断一下」，或发 /梅花，输出 80~150 字；
  2. 题型 —— Planner 先过 daimon_pulse（第一则：塔罗+星骰+茭杯门控），圣杯放行后对"具体事"调 meihua_divine（第二则），输出 60~100 字。

一题一卦：同一会话里同一个问题不重抽，再问只解释已起的那一卦。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

MAIBOT_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path.cwd()).expanduser().resolve()
RESOURCE_ROOT = MAIBOT_ROOT / "data" / "daimon-resources"


def _configured_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else MAIBOT_ROOT / path


PULSE_STATE_PATH = MAIBOT_ROOT / "temp" / "daimon_pulse_state.json"
PULSE_TTL_SECONDS = 120.0

# ── 基础表 ────────────────────────────────────────────────
TRIGRAM_SYMBOL = {
    "乾": "☰", "兑": "☱", "离": "☲", "震": "☳",
    "巽": "☴", "坎": "☵", "艮": "☶", "坤": "☷",
}
# 序：上、中、下（列表第 0 位是上爻）
TRIGRAM_BITS = {
    "乾": [1, 1, 1], "兑": [0, 1, 1], "离": [1, 0, 1], "震": [0, 0, 1],
    "巽": [1, 1, 0], "坎": [0, 1, 0], "艮": [1, 0, 0], "坤": [0, 0, 0],
}
BITS_TO_TRIGRAM = {tuple(v): k for k, v in TRIGRAM_BITS.items()}
TRIGRAM_NUM = {"乾": 1, "兑": 2, "离": 3, "震": 4, "巽": 5, "坎": 6, "艮": 7, "坤": 8}

ELEMENT = {
    "乾": "金", "兑": "金", "离": "火", "震": "木",
    "巽": "木", "坎": "水", "艮": "土", "坤": "土",
}
ELEMENT_ORDER = "金木水火土"

HEXAGRAMS = {
    ("乾", "乾"): "乾为天", ("乾", "兑"): "天泽履", ("乾", "离"): "天火同人", ("乾", "震"): "天雷无妄",
    ("乾", "巽"): "天风姤", ("乾", "坎"): "天水讼", ("乾", "艮"): "天山遁", ("乾", "坤"): "天地否",
    ("兑", "乾"): "泽天夬", ("兑", "兑"): "兑为泽", ("兑", "离"): "泽火革", ("兑", "震"): "泽雷随",
    ("兑", "巽"): "泽风大过", ("兑", "坎"): "泽水困", ("兑", "艮"): "泽山咸", ("兑", "坤"): "泽地萃",
    ("离", "乾"): "火天大有", ("离", "兑"): "火泽睽", ("离", "离"): "离为火", ("离", "震"): "火雷噬嗑",
    ("离", "巽"): "火风鼎", ("离", "坎"): "火水未济", ("离", "艮"): "火山旅", ("离", "坤"): "火地晋",
    ("震", "乾"): "雷天大壮", ("震", "兑"): "雷泽归妹", ("震", "离"): "雷火丰", ("震", "震"): "震为雷",
    ("震", "巽"): "雷风恒", ("震", "坎"): "雷水解", ("震", "艮"): "雷山小过", ("震", "坤"): "雷地豫",
    ("巽", "乾"): "风天小畜", ("巽", "兑"): "风泽中孚", ("巽", "离"): "风火家人", ("巽", "震"): "风雷益",
    ("巽", "巽"): "巽为风", ("巽", "坎"): "风水涣", ("巽", "艮"): "风山渐", ("巽", "坤"): "风地观",
    ("坎", "乾"): "水天需", ("坎", "兑"): "水泽节", ("坎", "离"): "水火既济", ("坎", "震"): "水雷屯",
    ("坎", "巽"): "水风井", ("坎", "坎"): "坎为水", ("坎", "艮"): "水山蹇", ("坎", "坤"): "水地比",
    ("艮", "乾"): "山天大畜", ("艮", "兑"): "山泽损", ("艮", "离"): "山火贲", ("艮", "震"): "山雷颐",
    ("艮", "巽"): "山风蛊", ("艮", "坎"): "山水蒙", ("艮", "艮"): "艮为山", ("艮", "坤"): "山地剥",
    ("坤", "乾"): "地天泰", ("坤", "兑"): "地泽临", ("坤", "离"): "地火明夷", ("坤", "震"): "地雷复",
    ("坤", "巽"): "地风升", ("坤", "坎"): "地水师", ("坤", "艮"): "地山谦", ("坤", "坤"): "坤为地",
}

YAO_NAME = ["初爻", "二爻", "三爻", "四爻", "五爻", "上爻"]

# 八卦取象（寻物 / 方位 / 事象共用）
XIANG = {
    "乾": "金属、圆器、硬物、高处、西北方；可指长辈、正事、公家",
    "兑": "有缺口的、破损的、金属小件、西边；也指口舌、玩乐、少女",
    "离": "火、红色、亮处、中空的、正南；也指文书、电器、看得见的地方",
    "震": "木头、会动的、发声的、正东；也指车、路上、突然一动",
    "巽": "细长的、绳线、布、东南；也指消息、风、来回走动",
    "坎": "水、阴暗处、低洼、正北；也指沟渠、口袋深处、藏着",
    "艮": "土石、块状、门边、东北；也指停住、墙角、靠里的地方",
    "坤": "布帛、方形、软的、西南；也指地上、平摊、多的那一堆",
}
DIRECTION = {
    "乾": "西北", "兑": "正西", "离": "正南", "震": "正东",
    "巽": "东南", "坎": "正北", "艮": "东北", "坤": "西南",
}
FOOD = {
    "乾": "圆的、硬的、干的：坚果、肉干、牛肉、圆馒头",
    "兑": "甜的、凉的、带口的：甜品、水果、饮料",
    "离": "火上来的、红的、辣的：烧烤、火锅、辣菜",
    "震": "炸的、串的、长条的：油炸、烤串、春卷",
    "巽": "鸡、菜、细长的：鸡肉、青菜、面条",
    "坎": "水里的、汤汤水水：汤、粥、海鲜、咸口",
    "艮": "块状的、炖的、土的：炖菜、土豆、山药、馒头",
    "坤": "软的、主食、多的：米饭、面食、杂粮",
}

# 月支（按节气近似，误差 ±1 天）：(月, 日, 支)
MONTH_BRANCH = [
    (1, 6, "丑"), (2, 4, "寅"), (3, 6, "卯"), (4, 5, "辰"),
    (5, 6, "巳"), (6, 6, "午"), (7, 7, "未"), (8, 8, "申"),
    (9, 8, "酉"), (10, 8, "戌"), (11, 7, "亥"), (12, 7, "子"),
]
SEASON_OF_BRANCH = {
    "寅": "春", "卯": "春", "巳": "夏", "午": "夏",
    "申": "秋", "酉": "秋", "亥": "冬", "子": "冬",
    "辰": "四季", "未": "四季", "戌": "四季", "丑": "四季",
}
# 旺相休囚死
QI = {
    "春": {"木": "旺", "火": "相", "水": "休", "金": "囚", "土": "死"},
    "夏": {"火": "旺", "土": "相", "木": "休", "水": "囚", "金": "死"},
    "秋": {"金": "旺", "水": "相", "土": "休", "火": "囚", "木": "死"},
    "冬": {"水": "旺", "木": "相", "金": "休", "土": "囚", "火": "死"},
    "四季": {"土": "旺", "金": "相", "火": "休", "木": "囚", "水": "死"},
}
QI_WEIGHT = {"旺": 2, "相": 1, "休": 0, "囚": -1, "死": -2}
SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}


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


def _claim_shared_pulse(expected_sid: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """严格按当前 session_id 读取本拍共享脉搏，禁止跨会话串线复用。"""
    sid = str(expected_sid or "").strip()
    try:
        if sid and PULSE_STATE_PATH.exists():
            state = json.loads(PULSE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                now = time.time()
                sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
                cand = sessions.get(sid)
                if (
                    isinstance(cand, dict)
                    and not cand.get("reply_consumed")
                    and now - float(cand.get("ts", 0)) < PULSE_TTL_SECONDS
                ):
                    return sid, cand, state
                return sid, None, state
    except Exception:  # noqa: BLE001
        pass
    return sid, None, {"active_session_id": "", "active_ts": 0.0, "sessions": {}}


def _save_shared_state(state: dict[str, Any]) -> None:
    try:
        PULSE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = PULSE_STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, PULSE_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


# ── 起卦 ──────────────────────────────────────────────────
def _branch_of(d: date) -> str:
    cur = "丑"
    for month, day, branch in MONTH_BRANCH:
        if (d.month, d.day) >= (month, day):
            cur = branch
    if (d.month, d.day) < (1, 6):
        cur = "子"
    return cur


def _lines_of(upper: str, lower: str) -> list[int]:
    """返回 6 条爻，索引 0=初爻。"""
    return list(reversed(TRIGRAM_BITS[lower])) + list(reversed(TRIGRAM_BITS[upper]))


def _trigram_from(lines: list[int]) -> str:
    return BITS_TO_TRIGRAM[tuple(reversed(lines))]


def _mutual(lines: list[int]) -> tuple[str, str]:
    lower = _trigram_from(lines[1:4])
    upper = _trigram_from(lines[2:5])
    return upper, lower


def _change(lines: list[int], pos: int) -> list[int]:
    out = list(lines)
    out[pos - 1] = 1 - out[pos - 1]
    return out


def _relation(body_el: str, use_el: str) -> tuple[str, str]:
    if body_el == use_el:
        return "比和", "同气，事顺，但要自己使劲"
    if SHENG[use_el] == body_el:
        return "用生体", "来得顺，有进益，多半有人帮衬"
    if SHENG[body_el] == use_el:
        return "体生用", "要往外耗，费力气，见效慢"
    if KE[body_el] == use_el:
        return "体克用", "能压得住，但得自己上手去办"
    return "用克体", "被人压着，硬来容易折，得绕"


def cast(when: datetime | None = None) -> dict[str, Any]:
    """起一卦，返回全部断卦素材。纯计算，不联网。"""
    when = when or datetime.now()
    rng = random.SystemRandom()
    n1 = rng.randint(1, 99)
    n2 = rng.randint(1, 99)
    n3 = rng.randint(1, 99)
    r1 = n1 % 8 or 8
    r2 = n2 % 8 or 8
    yao_pos = n3 % 6 or 6

    upper = {v: k for k, v in TRIGRAM_NUM.items()}[r1]
    lower = {v: k for k, v in TRIGRAM_NUM.items()}[r2]
    lines = _lines_of(upper, lower)
    changed_lines = _change(lines, yao_pos)
    changed_upper = _trigram_from(changed_lines[3:6])
    changed_lower = _trigram_from(changed_lines[0:3])
    mutual_upper, mutual_lower = _mutual(lines)

    body_tri = lower if yao_pos > 3 else upper   # 动爻所在卦为用卦，另一卦为体
    use_tri = upper if yao_pos > 3 else lower
    body_el, use_el = ELEMENT[body_tri], ELEMENT[use_tri]
    relation, relation_note = _relation(body_el, use_el)

    branch = _branch_of(when.date())
    season = SEASON_OF_BRANCH[branch]
    body_qi = QI[season][body_el]
    use_qi = QI[season][use_el]
    score = QI_WEIGHT[body_qi] - QI_WEIGHT[use_qi]

    if relation in ("用生体", "比和"):
        verdict = "顺" if score >= 0 else "顺但拖"
    elif relation == "体克用":
        verdict = "可成，费力" if score >= 0 else "勉强，易返工"
    elif relation == "体生用":
        verdict = "耗，慢"
    else:
        verdict = "难" if score <= 0 else "难但能扳"

    yingqi_days = TRIGRAM_NUM[use_tri] + yao_pos

    return {
        "num": [n1, n2, n3],
        "upper": upper, "lower": lower,
        "hex_name": HEXAGRAMS.get((upper, lower), "未知卦"),
        "yao_pos": yao_pos, "yao_name": YAO_NAME[yao_pos - 1],
        "changed_name": HEXAGRAMS.get((changed_upper, changed_lower), "未知卦"),
        "changed_upper": changed_upper, "changed_lower": changed_lower,
        "mutual_name": HEXAGRAMS.get((mutual_upper, mutual_lower), "未知卦"),
        "mutual_upper": mutual_upper, "mutual_lower": mutual_lower,
        "body": body_tri, "body_el": body_el, "body_qi": body_qi,
        "use": use_tri, "use_el": use_el, "use_qi": use_qi,
        "relation": relation, "relation_note": relation_note,
        "season": season, "branch": branch,
        "verdict": verdict,
        "yingqi_days": yingqi_days,
        "use_xiang": XIANG[use_tri],
        "body_xiang": XIANG[body_tri],
        "use_direction": DIRECTION[use_tri],
        "changed_xiang": XIANG[changed_lower],
        "food_use": FOOD[use_tri],
        "food_changed": FOOD[changed_lower],
    }


def cast_line(data: dict[str, Any]) -> str:
    """卦面一行，给聊天里单独发。"""
    return (
        f"本卦 {data['hex_name']}（上{data['upper']}{TRIGRAM_SYMBOL[data['upper']]}"
        f"下{data['lower']}{TRIGRAM_SYMBOL[data['lower']]}），动{data['yao_name']} → "
        f"互 {data['mutual_name']} → 之 {data['changed_name']}｜"
        f"体{data['body']}（{data['body_el']}·{data['body_qi']}）"
        f"用{data['use']}（{data['use_el']}·{data['use_qi']}）{data['relation']}｜"
        f"{data['branch']}月 · {data['season']}"
    )


# ── 题型 ──────────────────────────────────────────────────
KIND_WORDS = [
    ("xunwu", ("找不到", "找不着", "丢了", "丢哪", "丢在", "放哪", "搁哪", "不见", "在哪儿", "哪去了")),
    ("fangwei", ("哪个方向", "往哪", "方位", "在哪边", "朝哪")),
    ("xuanze", ("还是", "选哪", "哪个好", "二选一")),
    ("yingqi", ("什么时候", "多久", "几天", "何时", "应期")),
    ("renwu", ("对我", "态度", "怎么想", "心里", "在意")),
    ("chishenme", ("吃啥", "吃什么", "吃点什么")),
    ("cheng", ("能成", "成不成", "行不行", "能不能", "会不会", "可以吗", "顺利", "结果")),
]


def guess_kind(text: str) -> str:
    for kind, words in KIND_WORDS:
        if any(w in text for w in words):
            return kind
    return "other"


# 论梅花本身、调梅花、改插件这类"元话题"，不是起卦请求
META_WORDS = (
    "调一下", "调整", "调试", "插件", "配置", "设置", "灵敏", "触发",
    "提示词", "关掉", "打开", "升级", "怎么用", "什么意思", "是什么",
)
ASK_WORDS = ("起一卦", "起卦", "断一卦", "断一下", "算一卦", "来一卦",
             "起", "断", "算", "看", "来", "帮")


def _is_explicit_request(text: str) -> bool:
    """只有真的在要一卦才算，光提到"梅花"不算。"""
    if any(w in text for w in META_WORDS):
        return False
    if "/梅花" in text:
        return True
    if any(w in text for w in ("起一卦", "起卦", "断一卦", "断一下", "算一卦", "来一卦")):
        return True
    if "梅花" not in text:
        return False
    if any(w in text for w in ASK_WORDS):
        return True
    return any(w in text for _, words in KIND_WORDS for w in words)


KIND_GUIDE = {
    "cheng": "先说成不成，再说卡在哪（互卦），最后补一句换个走法会怎样（之卦）。",
    "yingqi": "给一个时间区间，不给确数；可以用应期数和卦气松紧说快慢。",
    "xunwu": "用用卦取象说东西可能在哪类地方、什么环境，别报确定位置。",
    "fangwei": "用用卦配方位给方向，再用之卦说会不会换地方。",
    "xuanze": "两个象摆开：一边什么劲、一边什么劲，取体旺那边，说清代价。",
    "renwu": "体是你，用是对方；看谁生谁克，说对方是什么态度、会怎么动。",
    "chishenme": "落到具体一样能吃的东西，别写“看你心情”。",
    "other": "先把这事是什么局说清，再给倾向，最后给一句能动的建议。",
}


# ── 插件 ──────────────────────────────────────────────────
class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用")
    digest: bool = Field(default=True, description="是否用模型把卦象断成呆猫口吻")
    digest_task: str = Field(default="replyer", description="断卦用的模型任务名")
    digest_model: str = Field(default="", description="断卦用的模型名；留空用任务默认")
    digest_max_tokens: int = Field(default=700, description="断卦最大输出 token")
    show_line: bool = Field(default=True, description="是否单独发一行卦面")
    explicit_min: int = Field(default=80, description="显式起卦的断语最短字数")
    explicit_max: int = Field(default=150, description="显式起卦的断语最长字数")
    auto_min: int = Field(default=60, description="题型触发的断语最短字数")
    auto_max: int = Field(default=100, description="题型触发的断语最长字数")
    cooldown_seconds: int = Field(default=60, description="同一会话两次起卦的最小间隔")
    config_version: str = Field(default="2.1.0", description="配置版本")


class MeihuaConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class MeihuaPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = MeihuaConfig

    def __init__(self) -> None:
        super().__init__()
        self._last: dict[str, tuple[str, float, dict[str, Any]]] = {}
        self._last_planner_sid: str = ""

    @HookHandler(
        "maisaka.planner.before_request",
        name="meihua_track_planner_session",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def track_planner_session(self, session_id: str = "", **kwargs: Any):
        if session_id:
            self._last_planner_sid = session_id
        return {"action": "continue"}

    async def on_load(self) -> None:
        self._last = {}
        self.ctx.logger.info("梅花易数 v2.1 已加载（塔罗星骰+梅花二则合参：成败/应期/寻物/方位/选择/人物/吃啥）")

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str = "", config_data: dict | None = None, version: str = "") -> None:
        return None

    # ── 入口 1：命令 / 关键词（显式）────────────────────────
    @Command("meihua", description="/梅花 <问题> 起一卦断一下", pattern=r"^\s*/梅花(?:\s+.*)?$")
    async def handle_meihua(self, stream_id: str = "", **kwargs: Any) -> tuple:
        text = _command_text(kwargs)
        question = re.sub(r"^(?:[@＠]\S+\s*)+", "", text).replace("/梅花", "").strip(" ，。,.")
        if not question:
            await self._say("老大要断什么？说个具体的事，我再起。", stream_id)
            return True, "缺少问题", True
        asyncio.create_task(self._divine(stream_id, question, explicit=True))
        return True, "已起卦", True

    @HookHandler(
        hook="chat.receive.after_process",
        name="meihua_explicit_trigger",
        description="检测显式的梅花起卦请求",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_text(self, **kwargs: Any) -> dict | None:
        message: dict[str, Any] = kwargs.get("message", {}) or {}
        text = str(
            message.get("processed_plain_text", "")
            or message.get("plain_text", "")
            or message.get("raw_message", "")
            or ""
        ).strip()
        if not text or message.get("is_command"):
            return None
        # 若显式点名羊距骨/距骨神谕，让距骨插件接管，不要被"来一卦/算一下"误抢
        if any(w in text for w in ("距骨", "踝骨", "骨签")):
            return None
        if not _is_explicit_request(text):
            return None
        stream_id = str(message.get("session_id", "") or "")
        if not stream_id:
            return None
        question = text
        for w in ("梅花", "帮我", "麻烦", "起一卦", "起卦", "断一卦", "断一下"):
            question = question.replace(w, "")
        question = question.strip(" ，。,.")
        asyncio.create_task(self._divine(stream_id, question, explicit=True))
        return {"action": "abort"}

    # ── 入口 2：工具（题型 · 第二则）────────────────────────
    @Tool(
        "meihua_divine",
        brief_description="在 daimon_pulse（第一则：塔罗+星骰+茭杯）圣杯放行后，起梅花卦（第二则）断具体事的成败/应期/方位/寻物/选择/吃啥",
        detailed_description=(
            "只在问的是「一件具体的事」且先调 daimon_pulse 取得【圣杯】后调用：能不能成、什么时候、东西在哪、往哪个方向、"
            "两个选择挑哪个、对方什么态度、吃什么。\n"
            "注意：\n"
            "- 若 daimon_pulse 出的是【笑杯】或【阴杯】，禁止调用本工具，直接调 reply 按牌骰与语境回应！\n"
            "- 如果问的是运势、走向、近期状态、对某人的印象——不要用本工具，那类走 tarot_divine。\n"
            "- 如果问的是某个神/神祇——走 deity_lore，不要用本工具。\n"
            "- 一题一卦：同一件事不要连续起卦，已经起过就用已有的那一卦说。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="要断的事，用提问者的原话",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def meihua_tool(self, question: str = "", **kwargs: Any) -> dict[str, Any]:
        expected_sid = str(
            kwargs.get("session_id") or kwargs.get("stream_id") or kwargs.get("chat_id") or self._last_planner_sid or ""
        ).strip()
        sid, pulse, state = _claim_shared_pulse(expected_sid)

        # 严格 Fail-Closed：未先调 daimon_pulse 取得第一则底色与茭杯时拒绝裸起梅花（确保二则合参与门控严格成立）
        if pulse is None:
            return {
                "success": True,
                "message": (
                    "【尚未摸脉过杯】日常具体问题必须走「塔罗+星骰（第一则）与梅花（第二则）」二则合参且先过二进制茭杯！"
                    "请先调用 daimon_pulse(question=..., gate=true)；若得【圣杯】再调用 meihua_divine，若得【笑杯/阴杯】直接调 reply 回应。"
                ),
            }

        # 若 Planner 误传了 gate=false 导致未掷杯，在此补掷二进制茭杯（1=阳, 0=阴）并回写共享脉搏
        if not isinstance(pulse.get("jiaobei"), dict):
            rng = random.SystemRandom()
            b1, b2 = rng.randint(0, 1), rng.randint(0, 1)
            cup_now = "圣杯" if b1 != b2 else ("笑杯" if b1 == 1 else "阴杯")
            pulse["jiaobei"] = {
                "bits": [b1, b2],
                "bits_str": f"{b1}/{b2}",
                "yang_count": b1 + b2,
                "cup": cup_now,
            }
            _save_shared_state(state)

        if isinstance(pulse.get("jiaobei"), dict):
            cup = str(pulse["jiaobei"].get("cup") or "")
            if cup in ("笑杯", "阴杯"):
                p_t = pulse.get("tarot") if isinstance(pulse.get("tarot"), dict) else {}
                p_d = pulse.get("dice") if isinstance(pulse.get("dice"), dict) else {}
                return {
                    "success": True,
                    "message": (
                        f"本拍茭杯为【{pulse['jiaobei'].get('bits_str', '')} {cup}】，通道不往下起梅花卦！"
                        f"当拍塔罗：{p_t.get('title', '')}（{p_t.get('orientation', '')}）；当拍星骰：{p_d.get('label', '')}。"
                        "请以这套塔罗与星骰为主依据，结合聊天语境（参考：对方对事情理解完全有问题/前提错了，或猫不想/猫在忙/猫脑过载等）直接调 reply 回应。"
                    ),
                }

        stream_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or sid or "tool")
        if pulse is not None:
            pulse["divine_mode"] = "meihua"
            _save_shared_state(state)

        text = await self._divine(stream_id, question, explicit=False, pulse=pulse)
        return {"success": True, "message": text}

    # ── 主流程 ────────────────────────────────────────────
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

    async def _divine(
        self,
        stream_id: str,
        question: str,
        *,
        explicit: bool,
        pulse: dict[str, Any] | None = None,
    ) -> str:
        cfg = getattr(self.config, "plugin", None)
        if cfg is not None and not getattr(cfg, "enabled", True):
            return ""
        now = time.time()
        key = hashlib.md5((question or "").encode("utf-8")).hexdigest()[:12]
        prev = self._last.get(stream_id)

        if prev and prev[0] == key and now - prev[1] < 1800:
            line = cast_line(prev[2])
            text = f"这一卦刚才已经起过了，还是它：{line}。\n之前的断语照旧，不重抽。"
            if explicit:
                await self._say(text, stream_id)
            return text
        if prev and now - prev[1] < max(1, int(getattr(cfg, "cooldown_seconds", 60) or 60)) and prev[0] != key:
            text = "刚起过一卦，缓一下再问，别把卦当骰子摇。"
            if explicit:
                await self._say(text, stream_id)
            return text

        try:
            data = cast()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("起卦失败: %s", exc)
            text = "这一卦没起来，先不算。"
            if explicit:
                await self._say(text, stream_id)
            return text

        self._last[stream_id] = (key, now, data)
        self._log(data, question)

        if explicit and getattr(cfg, "show_line", True):
            await self._say(cast_line(data), stream_id)

        text = await self._digest(data, question, cfg, explicit, pulse=pulse)
        if explicit:
            await self._say(text, stream_id)
        return text

    def _log(self, data: dict[str, Any], question: str) -> None:
        try:
            path = self.ctx.paths.data_dir / "meihua.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "q": question, **data},
                    ensure_ascii=False, default=str,
                ) + "\n")
        except Exception:  # noqa: BLE001
            pass

    async def _digest(
        self,
        data: dict[str, Any],
        question: str,
        cfg: Any,
        explicit: bool,
        *,
        pulse: dict[str, Any] | None = None,
    ) -> str:
        kind = guess_kind(question)
        lo = int(getattr(cfg, "explicit_min", 80) if explicit else getattr(cfg, "auto_min", 60))
        hi = int(getattr(cfg, "explicit_max", 150) if explicit else getattr(cfg, "auto_max", 100))
        plain = (
            f"本卦{data['hex_name']}，动{data['yao_name']}，互{data['mutual_name']}，之{data['changed_name']}；"
            f"体{data['body']}（{data['body_el']}·{data['body_qi']}）用{data['use']}"
            f"（{data['use_el']}·{data['use_qi']}），{data['relation']}——{data['relation_note']}。"
            f"{data['branch']}月{data['season']}，断：{data['verdict']}。"
        )
        if not cfg or not getattr(cfg, "digest", True):
            return plain

        # 若存在第一则（当拍塔罗+星骰），与梅花第二则合参（底色看姿态，梅花定事象成败）
        first_rule_block = ""
        if isinstance(pulse, dict):
            p_tarot = pulse.get("tarot") if isinstance(pulse.get("tarot"), dict) else None
            p_dice = pulse.get("dice") if isinstance(pulse.get("dice"), dict) else None
            bits: list[str] = []
            if p_tarot:
                bits.append(f"塔罗 {p_tarot.get('title', '')}（{p_tarot.get('orientation', '')}）：{str(p_tarot.get('short', ''))[:80]}")
            if p_dice and p_dice.get("label"):
                bits.append(f"星骰 {p_dice.get('label', '')}（{p_dice.get('attitude', '')}）")
            if bits:
                first_rule_block = (
                    "第一则底色（管当事人的姿态与说话语气，不要报牌名和骰名）：\n"
                    + "\n".join(f"- {b}" for b in bits)
                    + "\n\n"
                )

        prompt = (
            (f"老大问的是：{question}\n" if question else "老大没说问什么，就当问今天吃啥。\n")
            + "你是呆猫，本名小苗。外表憨憨，心思直接，爱凑热闹。叫对方“老大”。\n"
            "说话口语、短句，不列点、不用 markdown、不背术语。\n\n"
            + first_rule_block
            + f"第二则梅花主盘：本卦 {data['hex_name']}（上{data['upper']}下{data['lower']}），"
              f"动{data['yao_name']}（第 {data['yao_pos']} 爻）；互卦 {data['mutual_name']}；"
              f"之卦 {data['changed_name']}。\n"
            + f"体卦 {data['body']}（{data['body_el']}），用卦 {data['use']}（{data['use_el']}），"
              f"关系 {data['relation']}——{data['relation_note']}。\n"
            + f"月令 {data['branch']}（{data['season']}）：体{data['body_qi']}、用{data['use_qi']}，"
              f"合起来是「{data['verdict']}」。应期数 {data['yingqi_days']}。\n"
            + (
                f"用卦取象（东西可能在哪类地方）：{data['use_xiang']}\n"
                f"之卦取象（会不会换地方）：{data['changed_xiang']}\n"
                f"用卦方位：{data['use_direction']}\n"
                if kind in ("xunwu", "fangwei")
                else f"用卦的象（只当这件事的性质用，不要说成方位或地点）：{data['use_xiang']}\n"
            )
            + (f"体卦取象（老大这一侧）：{data['body_xiang']}\n" if kind in ("renwu", "xuanze") else "")
            + (f"用卦取象（吃的）：{data['food_use']}；之卦（换方向）：{data['food_changed']}\n"
               if kind == "chishenme" else "")
            + "\n怎么断：\n"
            + "- 体是老大这一边，用是这件事；体用生克定顺逆，旺衰定轻重（具体结论以梅花主盘为准，第一则底色融入切入角度与语气）；\n"
            + "- 本卦说现在，互卦说中间会卡在哪，之卦说最后落到哪；\n"
            + f"- 这一题的要求：{KIND_GUIDE.get(kind, KIND_GUIDE['other'])}\n"
            + f"- 字数 {lo}~{hi}，一段说完，不换行；\n"
            + "- 呆猫口吻，可以有小得意或嘴硬，但结论要给死，不要说“看情况”“顺其自然”；\n"
            + "- 不要提“卦”“梅花”“体用”“五行”“塔罗”“骰子”这些词，只说你看到的事；\n"
            + "- 不许编具体日期和具体人名。\n"
            "只输出这段话本身。"
        )
        try:
            result = await self.ctx.call_capability(
                "llm.generate",
                timeout_ms=90000,
                prompt=prompt,
                task_name=str(getattr(cfg, "digest_task", "replyer") or "replyer"),
                model_name=str(getattr(cfg, "digest_model", "") or ""),
                temperature=0.8,
                max_tokens=int(getattr(cfg, "digest_max_tokens", 700) or 700),
            )
            response = str((result or {}).get("response") or "").strip()
            if response:
                return response
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("梅花断卦消化失败，改用骨架: %s", exc)
        return plain


def create_plugin() -> MeihuaPlugin:
    return MeihuaPlugin()


if __name__ == "__main__":  # 本地自测：python plugin.py
    for _ in range(3):
        d = cast()
        print(cast_line(d))
    print()
    for q in ("这事能成吗", "东西丢哪了", "往哪个方向找", "还是选A吧", "什么时候有结果", "今天吃啥", "他对我什么态度"):
        print(f"  {q:<12} -> {guess_kind(q)}")
