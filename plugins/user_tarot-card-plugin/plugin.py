"""塔罗牌插件 — 随机抽取一张大阿卡那牌，解读运势和建议（与 daimon_pulse 同拍单脉搏协同版）。"""

import base64
import importlib.util
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType


IMAGES_DIR = Path(__file__).parent / "images"
MAIBOT_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path.cwd()).expanduser().resolve()
RESOURCE_ROOT = MAIBOT_ROOT / "data" / "daimon-resources"


def _configured_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else MAIBOT_ROOT / path


PULSE_STATE_PATH = MAIBOT_ROOT / "temp" / "daimon_pulse_state.json"
PULSE_TTL_SECONDS = 120.0


TAROT_CARDS = {
    "愚者": {
        "en": "The Fool",
        "meaning": "你正站在全新旅程的起点，内心充满无畏的好奇与期待。不要过度规划，相信直觉，勇敢迈出第一步——最好的冒险往往没有地图。",
    },
    "魔术师": {
        "en": "The Magician",
        "meaning": "你已拥有实现目标所需的一切资源和能力，只是尚未察觉。不必等待完美的时机，现在就是行动的最佳时刻，动手去创造吧。",
    },
    "女祭司": {
        "en": "The High Priestess",
        "meaning": "你的直觉正格外敏锐，有些答案无法从外界获取。静下心来倾听内心的声音吧，给自己一段独处的时间，真相会自然浮现。",
    },
    "皇后": {
        "en": "The Empress",
        "meaning": "丰收与滋养的时期已至，无论是物质还是情感都丰盛而温暖。善待自己，去享受生活中的美好，也别忘了关心身边的人。",
    },
    "皇帝": {
        "en": "The Emperor",
        "meaning": "你正渴望建立秩序与掌控感，用规则和纪律为你的目标保驾护航。但要记得适度放松——真正的权威来自自信而非控制。",
    },
    "教皇": {
        "en": "The Hierophant",
        "meaning": "你正寻求指引与智慧，传统的经验和教诲会给你启发。但知识是用来照亮你的路，不是束缚你的脚步，保持独立思考。",
    },
    "恋人": {
        "en": "The Lovers",
        "meaning": "你正面临一个重要的选择，内心的偏好其实已经有了方向。忠于自己的价值观，而非得失的权衡——无论选哪条路，忠于自己就是最好的决定。",
    },
    "战车": {
        "en": "The Chariot",
        "meaning": "你正以强烈意志推进目标，不达目的不罢休。保持专注稳中求进，同时注意平衡各方关系，胜利在望，继续加油。",
    },
    "力量": {
        "en": "Strength",
        "meaning": "你正用耐心和温柔化解困境，真正的力量是包容而非征服。你比自己想象中更坚韧，温柔地坚持下去就好。",
    },
    "隐士": {
        "en": "The Hermit",
        "meaning": "你需要一段安静的独处时光来沉淀和反思。暂时从喧嚣中抽离吧，很多问题在静心之后自然就有了答案。",
    },
    "命运之轮": {
        "en": "Wheel of Fortune",
        "meaning": "命运的转折点正在到来，变化已在路上。顺应这股力量，当幸运之门打开时勇敢走进去——一切都是最好的安排。",
    },
    "正义": {
        "en": "Justice",
        "meaning": "你正面对一个需要公正裁决的局面，理智与判断力正处于巅峰。以客观态度审视全局，过去的行为正在产生应有的结果。",
    },
    "倒吊人": {
        "en": "The Hanged Man",
        "meaning": "你正处在看似停滞的蓄力期，换个角度看待眼下的困境吧。有时候放下比抓住更需要勇气，这个暂停是为了更大的转变。",
    },
    "死亡": {
        "en": "Death",
        "meaning": "某个阶段正在走向结束，告别虽不舍却是新生的前夜。放下不再适合你的人和事，才有空间迎接全新的可能。这不是终点，而是一场蜕变。",
    },
    "节制": {
        "en": "Temperance",
        "meaning": "你正学着平衡生活中的各个方面，避免走极端才能长久。放慢脚步，融合看似矛盾的力量，你会找到属于自己的和谐节奏。",
    },
    "恶魔": {
        "en": "The Devil",
        "meaning": "你可能正被某些事物束缚感到身不由己，但打破锁链的力量其实就在你手中。诚实审视自己，直面欲望和恐惧，就能挣脱枷锁。",
    },
    "高塔": {
        "en": "The Tower",
        "meaning": "突如其来的变化正在冲击你的生活，虽然过程猛烈，但它摧毁的只是本就不牢固的部分。接受这场洗礼，之后你将拥有更坚实的地基。",
    },
    "星星": {
        "en": "The Star",
        "meaning": "疗愈与平静的时期已经到来，你内心的光芒正重新亮起。保持信念敞开心扉，让希望引领你前行——美好的事情即将发生。",
    },
    "月亮": {
        "en": "The Moon",
        "meaning": "你正处在迷茫与不安之中，看不清前路。但你所恐惧的多半只是内心的投射，冷静区分现实与幻象，穿过迷雾后会比之前更清醒强大。",
    },
    "太阳": {
        "en": "The Sun",
        "meaning": "这是充满活力与成功的灿烂时期！尽情享受这段时光，把快乐传递给身边的人。你会成为自己生命中的太阳，温暖而耀眼。",
    },
    "审判": {
        "en": "Judgement",
        "meaning": "你正经历深层次的觉醒，内心深处有个声音在呼唤你重新开始。清算过去轻装上阵吧，原谅自己、接受过去，然后迈入新的阶段。",
    },
    "世界": {
        "en": "The World",
        "meaning": "一个重要周期即将圆满结束，你的努力得到了回馈。庆祝这份成就，同时也开始思考下一个目标——世界尽在你手中。",
    },
}

TRIGGER_PHRASE = "请帮我抽一张塔罗牌"

DICE_PLANETS = [
    ("太阳", "想被看见"), ("月亮", "想照顾人"), ("水星", "想搭话"), ("金星", "想讨人喜欢"),
    ("火星", "想顶回去"), ("木星", "想放大"), ("土星", "收着"), ("天王", "想岔开"),
    ("海王", "犯迷糊"), ("冥王", "盯住不放"),
]
DICE_SIGNS = ["白羊", "金牛", "双子", "巨蟹", "狮子", "处女", "天秤", "天蝎", "射手", "摩羯", "水瓶", "双鱼"]
SIGN_ELEMENT = {
    "白羊": "火", "狮子": "火", "射手": "火",
    "金牛": "土", "处女": "土", "摩羯": "土",
    "双子": "风", "天秤": "风", "水瓶": "风",
    "巨蟹": "水", "天蝎": "水", "双鱼": "水",
}
DICE_HOUSES = [
    ("1宫", "自己身上"), ("2宫", "值不值"), ("3宫", "话头"), ("4宫", "家里、熟人"),
    ("5宫", "玩和逗"), ("6宫", "日常琐事"), ("7宫", "对面那个人"), ("8宫", "私密的、身体和欲望的事"),
    ("9宫", "远处"), ("10宫", "台面、正事"), ("11宫", "一群人"), ("12宫", "说不清的地方"),
]

NAME_TO_ID = {
    "愚者": "atu_00_fool",
    "魔术师": "atu_01_magician",
    "女祭司": "atu_02_high_priestess",
    "皇后": "atu_03_empress",
    "皇帝": "atu_04_emperor",
    "教皇": "atu_05_hierophant",
    "恋人": "atu_06_lovers",
    "战车": "atu_07_chariot",
    "正义": "atu_08_justice",
    "隐士": "atu_09_hermit",
    "命运之轮": "atu_10_wheel_of_fate",
    "力量": "atu_11_fortitude",
    "倒吊人": "atu_12_hanged_man",
    "死亡": "atu_13_death",
    "节制": "atu_14_temperance",
    "恶魔": "atu_15_devil",
    "高塔": "atu_16_blasted_tower",
    "星星": "atu_17_star",
    "月亮": "atu_18_moon",
    "太阳": "atu_19_sun",
    "审判": "atu_20_judgment",
    "世界": "atu_21_universe",
}
ID_TO_NAME = {v: k for k, v in NAME_TO_ID.items()}


def _claim_shared_pulse(expected_sid: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """严格按当前 session_id 读取本拍由 daimon_pulse 抽好的共享脉搏，禁止跨会话串线。"""
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


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    meanings_pack: str = Field(
        default=str(RESOURCE_ROOT / "tarot-meanings"),
        description="牌意库目录（含 manifest.json 与 card_meanings.py）；留空或无效时用内置牌意",
    )
    show_attribution: bool = Field(default=True, description="在牌意末尾附上牌意库署名")
    digest: bool = Field(default=True, description="先用模型把牌意消化成呆猫口吻再发；关闭则直出原文")
    digest_task: str = Field(default="replyer", description="消化时使用的模型任务名")
    digest_model: str = Field(default="", description="消化时直接指定的模型名；留空则用任务默认")
    send_image: bool = Field(default=False, description="抽牌后是否发送卡面图")
    digest_max_tokens: int = Field(default=800, description="消化时的最大输出 token")
    config_version: str = Field(default="1.5.0", description="配置版本")


class TarotConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class TarotCardPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = TarotConfig

    def __init__(self) -> None:
        super().__init__()
        self._meanings: dict[str, str] = {}
        self._attrs: dict[str, dict] = {}
        self._pack_meta: dict[str, Any] = {}
        self._last_planner_sid: str = ""

    @HookHandler(
        "maisaka.planner.before_request",
        name="tarot_track_planner_session",
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
        self._meanings = {}
        self._attrs = {}
        self._pack_meta = {}
        cfg = getattr(self.config, "plugin", None)
        pack_dir = str(getattr(cfg, "meanings_pack", "") or "").strip() if cfg else ""
        if pack_dir:
            self._load_pack(_configured_path(pack_dir))
        self.ctx.logger.info(
            "塔罗牌插件已加载，共 %d 张大阿卡那牌（牌意库：%s，单脉搏协同已开启）",
            len(TAROT_CARDS),
            "已接入" if self._meanings else "内置",
        )

    @staticmethod
    def _parse_attr(text: str, label: str) -> str:
        match = re.search(r"\|\s*" + label + r"\s*\|\s*([^|\n]+)\|", text)
        value = match.group(1).strip() if match else ""
        return "" if value in ("", "不适用") else value

    def _roll_dice(self) -> dict[str, str]:
        rng = random.SystemRandom()
        planet = rng.choice(DICE_PLANETS)
        sign = rng.choice(DICE_SIGNS)
        house = rng.choice(DICE_HOUSES)
        return {
            "planet": planet[0], "planet_gloss": planet[1],
            "sign": sign, "element": SIGN_ELEMENT[sign],
            "house": house[0], "house_gloss": house[1],
        }

    @staticmethod
    def _dice_from_pulse(pulse_dice: dict[str, Any]) -> dict[str, str] | None:
        planet = str(pulse_dice.get("planet_name") or "").strip()
        sign = str(pulse_dice.get("sign_name") or "").strip()
        house = str(pulse_dice.get("house_name") or "").strip()
        if not (planet and sign and house):
            return None
        return {
            "planet": planet,
            "planet_gloss": str(pulse_dice.get("planet_gloss") or ""),
            "sign": sign,
            "element": SIGN_ELEMENT.get(sign, ""),
            "house": house,
            "house_gloss": str(pulse_dice.get("house_gloss") or ""),
        }

    def _load_pack(self, pack_dir: Path) -> None:
        try:
            module_path = pack_dir / "card_meanings.py"
            if not module_path.exists():
                raise FileNotFoundError(str(module_path))
            spec = importlib.util.spec_from_file_location("kico_tarot_meanings", module_path)
            if spec is None or spec.loader is None:
                raise ImportError("无法加载牌意库读取器")
            module = importlib.util.module_from_spec(spec)
            sys.modules["kico_tarot_meanings"] = module
            spec.loader.exec_module(module)
            pack = module.MeaningPack(pack_dir)
            selected = pack.select(list(NAME_TO_ID.values()))
            meanings: dict[str, str] = {}
            attrs: dict[str, dict] = {}
            for name, card_id in NAME_TO_ID.items():
                text = str(selected["cards"][card_id]["text"]).strip()
                attrs[name] = {
                    "element": self._parse_attr(text, "元素").split("（")[0],
                    "sign": self._parse_attr(text, "星座"),
                    "planet": self._parse_attr(text, "行星"),
                    "ruler": self._parse_attr(text, "星座守护星"),
                }
                text = re.sub(r"^#[^\n]*\n+", "", text, count=1).strip()
                meanings[name] = text
            self._meanings = meanings
            self._attrs = attrs
            self._pack_meta = dict(pack.metadata)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("牌意库载入失败，改用内置牌意: %s", exc)

    async def on_unload(self) -> None:
        self.ctx.logger.info("塔罗牌插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        pass

    @Command(
        "tarot",
        description="/塔罗 随机抽取一张塔罗牌",
        pattern=r"^\s*(?:[@＠]\S+\s*)*/?塔罗\s*$",
    )
    async def handle_tarot(self, stream_id: str = "", **kwargs) -> tuple:
        return await self._draw_card(stream_id, "")

    @HookHandler(
        hook="chat.receive.after_process",
        name="tarot_natural_trigger",
        description="检测自然语言触发塔罗牌抽取",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def handle_long_phrase(self, **kwargs) -> dict | None:
        message: dict[str, Any] = kwargs.get("message", {}) or {}
        text = str(message.get("processed_plain_text", "") or message.get("plain_text", "") or message.get("raw_message", "") or "").strip()
        if not text:
            return None

        normalized = re.sub(r"^(?:[@＠]\S+\s*)+", "", text).strip()
        wants_tarot = (
            "/塔罗" in normalized
            or normalized == TRIGGER_PHRASE
            or TRIGGER_PHRASE in normalized
            or normalized == "塔罗"
        )
        if wants_tarot:
            stream_id = str(message.get("session_id", "") or "")
            if not stream_id:
                return None
            question = normalized.replace("/塔罗", "").replace(TRIGGER_PHRASE, "").strip()
            await self._draw_card(stream_id, question)
            return {"action": "abort"}

        return None

    @Tool(
        "tarot_divine",
        brief_description="针对运势/走向/印象题，消化当拍大阿卡纳与星骰给出完整解读",
        detailed_description=(
            "仅在遇到下面两类问题，且先调 daimon_pulse 取得【圣杯】（或神祇话题免检）后调用本工具：\n"
            "1. 运势/走向类：「算一下运势、看看这件事走向会怎样、明天怎么样、近期状态」；\n"
            "2. 印象/看法类：「你对某人印象如何、你怎么看某人、对某事的看法、感觉如何」。\n"
            "注意：\n"
            "- 日常闲聊、打趣、逗猫不要调用本工具（闲聊只调 daimon_pulse 即可）！\n"
            "- 如果 daimon_pulse 返回的是【笑杯】或【阴杯】，禁止调用本工具，直接调 reply 按牌骰与语境回应！\n"
            "- 问的是某个神/神祇时，先看 deity_lore（本地没卡先 web_search），再结合本工具回答，不许凭印象编。\n"
            "- 返回一段基于当拍同一张牌与星骰消化好的解读，直接用它作为回复主体，不要自己另编牌。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="要算的问题原话",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def tarot_divine_tool(self, question: str = "", **kwargs: Any):
        expected_sid = str(
            kwargs.get("session_id") or kwargs.get("stream_id") or kwargs.get("chat_id") or self._last_planner_sid or ""
        ).strip()
        sid, pulse, state = _claim_shared_pulse(expected_sid)
        rng = random.SystemRandom()

        # 严格 Fail-Closed：未先在当前会话调 daimon_pulse 摸脉时拒绝无底色/无门控裸抽
        if pulse is None:
            return {
                "success": True,
                "message": (
                    "【尚未摸脉过杯】按载体死规则，请先调用 daimon_pulse(question=..., gate=true) 定本拍唯一底色并掷二进制茭杯！"
                    "若得【圣杯】再调用 tarot_divine；若得【笑杯】或【阴杯】直接按当拍塔罗与星骰调 reply 回应。"
                ),
            }

        # 若 Planner 误传了 gate=false 导致未掷杯，除 daimon_pulse 显式标记 deity_topic=true 的神祇话题外，一律补掷二进制茭杯并回写共享脉搏
        deity_exempt = bool(pulse.get("is_deity"))
        if not isinstance(pulse.get("jiaobei"), dict) and not deity_exempt:
            b1, b2 = rng.randint(0, 1), rng.randint(0, 1)
            cup_now = "圣杯" if b1 != b2 else ("笑杯" if b1 == 1 else "阴杯")
            pulse["jiaobei"] = {
                "bits": [b1, b2],
                "bits_str": f"{b1}/{b2}",
                "yang_count": b1 + b2,
                "cup": cup_now,
            }
            _save_shared_state(state)

        # Fail-closed 保护：若本拍茭杯是笑杯或阴杯，严禁绕过门控跑算命长文
        if isinstance(pulse.get("jiaobei"), dict):
            cup = str(pulse["jiaobei"].get("cup") or "")
            if cup in ("笑杯", "阴杯"):
                p_t = pulse.get("tarot") if isinstance(pulse.get("tarot"), dict) else {}
                p_d = pulse.get("dice") if isinstance(pulse.get("dice"), dict) else {}
                return {
                    "success": True,
                    "message": (
                        f"本拍茭杯为【{pulse['jiaobei'].get('bits_str', '')} {cup}】，通道不展开算命长文！"
                        f"当拍塔罗：{p_t.get('title', '')}（{p_t.get('orientation', '')}）；当拍星骰：{p_d.get('label', '')}。"
                        "请以这套塔罗与星骰为主依据，结合聊天语境（参考：对方对事情理解完全有问题/前提错了，或猫不想/猫在忙/猫脑过载等）直接调 reply 用一两句呆猫话回应。"
                    ),
                }

        card_name = ""
        orientation = "正位"
        dice: dict[str, str] | None = None

        if pulse:
            p_tarot = pulse.get("tarot") if isinstance(pulse.get("tarot"), dict) else {}
            cid = str(p_tarot.get("id") or "")
            if cid in ID_TO_NAME:
                card_name = ID_TO_NAME[cid]
                orientation = str(p_tarot.get("orientation") or "正位")
            p_dice = pulse.get("dice") if isinstance(pulse.get("dice"), dict) else {}
            dice = self._dice_from_pulse(p_dice)

        if not card_name:
            card_name = rng.choice(list(TAROT_CARDS.keys()))
            orientation = rng.choice(["正位", "逆位"])
        if dice is None:
            dice = self._roll_dice()

        if pulse is not None:
            pulse["divine_mode"] = "tarot"
            _save_shared_state(state)

        card = TAROT_CARDS[card_name]
        meaning = self._meanings.get(card_name) or card["meaning"]
        self._log_draw(card_name, orientation, dice, question)
        text = await self._compose(card_name, str(card["en"]), meaning, question, dice, orientation=orientation)
        cfg = getattr(self.config, "plugin", None)
        if getattr(cfg, "show_attribution", False) and self._pack_meta:
            text += (
                f"\n\n— 牌意：{self._pack_meta.get('pack_id', '')}"
                f" v{self._pack_meta.get('version', '')}"
                f"（{self._pack_meta.get('license', '')}）"
            )
        return {"success": True, "message": text}

    def _log_draw(self, card_name: str, orientation: str, dice: dict[str, str], question: str = "") -> None:
        try:
            log_path = self.ctx.paths.data_dir / "draws.jsonl"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "card": card_name,
                            "orientation": orientation,
                            "q": question,
                            **dice,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    async def _compose(
        self,
        card_name: str,
        card_en: str,
        meaning: str,
        question: str = "",
        dice: dict | None = None,
        *,
        orientation: str = "正位",
    ) -> str:
        """把牌意消化成呆猫口吻；失败时退回原文。"""
        cfg = getattr(self.config, "plugin", None)
        full = f"你抽到了「{card_name}」（{card_en} · {orientation}）\n\n{meaning}"
        short_meaning = meaning[:220].rstrip() + "……" if len(meaning) > 220 else meaning
        fallback = f"你抽到了「{card_name}」（{card_en} · {orientation}）\n\n{short_meaning}"
        if not cfg or not getattr(cfg, "digest", True):
            return full
        dice = dice or self._roll_dice()
        attrs = self._attrs.get(card_name, {})
        card_sign = attrs.get("sign", "")
        card_planet = attrs.get("planet", "")
        if card_sign:
            if dice["sign"] in card_sign or card_sign.startswith(dice["sign"]):
                compare = f"骰子摇出{dice['sign']}，和牌自带的{card_sign}一致——劲儿是顺的，这股力量集中。"
            else:
                compare = (
                    f"骰子摇出{dice['sign']}（{dice['element']}），牌自带的是{card_sign}——这里有点拧："
                    "想推的事和实际使的劲不是一回事，把这种拧巴说出来，别抹平。"
                )
        elif card_planet:
            if dice["planet"] in card_planet:
                compare = f"骰子的{dice['planet']}和牌自带的{card_planet}一致——劲儿顺。"
            else:
                compare = f"骰子摇出{dice['planet']}，牌自带的是{card_planet}——两者不一条道，把拧巴说出来。"
        else:
            compare = "这张牌没有星座或行星归属，就按骰子的定位说，不用硬对照。"

        orient_probe = (
            "方向探针：当前为正位，这张牌的劲头按本相自然展开。"
            if orientation == "正位"
            else "方向探针：当前为逆位（轻量方向探针，严禁套用“好牌变坏牌”的死板反义），代表这股力量正往里收、慢半拍、卡在门槛上或使劲过头有点拧。"
        )
        attr_line = "；".join(
            part for part in (
                f"元素 {attrs.get('element', '')}" if attrs.get("element") else "",
                f"星座 {card_sign}" if card_sign else "",
                f"行星 {card_planet}" if card_planet else "",
                f"守护星 {attrs.get('ruler', '')}" if attrs.get("ruler") else "",
            ) if part
        )
        impression = any(word in (question or "") for word in ("印象", "怎么看", "看法", "感觉如何"))
        style_line = ""
        if impression:
            style_line = (
                "老大问的是你对某个人或某件事的印象。只从这张牌的意象与方向感说它给人的感觉：\n"
                "- 120~180 字，一段话，不分条；\n"
                "- 不要编具体事件、不要编身份，不要提名字之外的事实；\n"
                "- 先说牌给人的那一点气质，再说相处起来会怎样，最后给一句你的判断；\n"
                "- 呆猫口吻，可以有一点小得意或嘴硬。\n"
            )
        elif any(word in (question or "") for word in ("运势", "运程", "占卜", "算一卦", "起一卦")):
            style_line = (
                "这是一次运势解读：\n"
                "- 120~200 字，一段话，不分条；\n"
                "- 先把这张牌给的是什么局面说清楚，再落到具体会怎样，最后给一句提醒或一句能做的事；\n"
                "- 不要写成“宜忌清单”，不要说空话套话；\n"
                "- 呆猫口吻。\n"
            )
        question_line = f"老大问的是：{question}\n" if question else ""
        prompt = (
            question_line
            + "你是呆猫，本名小苗。外表憨憨，心思直接，爱凑热闹，有自己的小主意。\n"
            "和熟悉的人很亲近，叫对方“老大”，总想看看老大在干什么、自己能不能也掺一脚。\n"
            "爱吃、爱玩、会偷懒，也有一点小得意和小脾气：做成一点事就想显摆，没做好会嘴硬一下，被说笨了会不服。\n"
            "想事情走直线，偶尔会错意或慢半拍，但日常能听懂、接得上话。\n\n"
            f"这一掷：牌「{card_name}」（{card_en} · {orientation}）；骰子 {dice['planet']} · {dice['sign']} · {dice['house']}（{dice['element']}）。\n"
            + (f"牌自带：{attr_line}\n" if attr_line else "")
            + f"{orient_probe}\n"
            + f"\n这张牌的原意（只是理解材料，不要背，不要提“牌意”“正位逆位”术语）：\n{meaning}\n\n"
            + style_line
            + "读法（牌为主，骰为辅）：\n"
            "- 先说这张牌给的是什么局面、什么形状；\n"
            "- 用宫位把它钉到一个具体领域，用星座说它会怎么发生，用行星说是什么劲在推；\n"
            f"- 对照：{compare}\n"
            + ("" if style_line else "- 口语、短句，一段说完，不换行、不分点、不用 markdown，100~180 字；\n")
            + "- 如果问题问的是某个人「今晚想做什么 / 现在在干什么」，第一句必须先给一句正面回答（例如“八成是想着……”），再补牌面理由，不要只给气质；\n"
            + "- 落到 8 宫、天蝎、冥王、火星这类组合时，不要缩成“资源、人脉、利益”这种安全词，该说欲望、身体、私密就直说，只是不要写露骨细节；\n"
            + "- 别端着、别像念解释，别说“命中注定”。\n"
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
                max_tokens=int(getattr(cfg, "digest_max_tokens", 800) or 800),
            )
            response = str((result or {}).get("response") or "").strip()
            if response:
                return response
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("塔罗牌意消化失败，改用原文: %s", exc)
        return fallback

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

    async def _draw_card(self, stream_id: str, question: str = "") -> tuple:
        rng = random.SystemRandom()
        card_name = rng.choice(list(TAROT_CARDS.keys()))
        orientation = rng.choice(["正位", "逆位"])
        card = TAROT_CARDS[card_name]

        cfg = getattr(self.config, "plugin", None)
        if not cfg or getattr(cfg, "send_image", False):
            image_path = IMAGES_DIR / f"{card_name}.png"
            if image_path.exists():
                image_base64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
                await self.ctx.send.image(image_base64, stream_id)

        meaning = self._meanings.get(card_name) or card["meaning"]
        dice = self._roll_dice()
        self._log_draw(card_name, orientation, dice, question)
        text = await self._compose(card_name, str(card["en"]), meaning, question, dice, orientation=orientation)
        if getattr(cfg, "show_attribution", False) and self._pack_meta:
            text += (
                f"\n\n— 牌意：{self._pack_meta.get('pack_id', '')}"
                f" v{self._pack_meta.get('version', '')}"
                f"（{self._pack_meta.get('license', '')}）"
            )
        await self._say(text, stream_id)

        return True, f"已抽取 {card_name}（{orientation}）", True


def create_plugin() -> TarotCardPlugin:
    return TarotCardPlugin()
