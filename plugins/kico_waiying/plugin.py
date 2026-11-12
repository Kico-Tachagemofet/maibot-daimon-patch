"""十字路口顺风耳 · 外应探针（kico.waiying v0.1）

设计要点
--------
1. 不建环形缓冲：判定时直接 `message.get_recent` 查最近 N 分钟的真实记录，
   省掉跨插件状态、进程隔离、重启丢数据这一整类问题。
2. 探针在 `chat.receive.after_process`（EARLY）里只做规则粗筛并起后台任务，
   绝不在 hook 里等模型（hook 有超时，会卡整条消息链）。
3. 受体不是"问题"，是"群里还悬着的那件事"——所以有人求测、没人求测都能响。
4. 出口只有一种：呆猫自己开口说一句「老大你看这个外应，XX 说的……」。
   点明"外应"是硬要求；不点私事、不解释机制。
5. 四道闸：群白/黑名单 → 粗筛 → 节流（探针/发言/日配额）→ 强度阈值。
   默认宁可漏，不许吵。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any, ClassVar, List

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# 结构化信号词：出现才值得看一眼（"被退/卡住/反悔/终于"这类带反转、停滞、成否的词）
STRUCT_CUES = (
    "被退", "退单", "退了", "退款", "黄了", "崩", "塌", "卡住", "卡在", "卡了",
    "拖着", "拖了", "没动静", "失联", "反悔", "变卦", "临时", "突然", "终于",
    "谈成", "签了", "吹了", "重来", "返工", "改期", "推迟", "提前", "抢", "砍",
    "撤", "放鸽子", "掉链子", "漏", "坏了", "断了", "摔", "丢了", "堵", "排队",
    "超时", "加急", "卖光", "断货", "补货", "改主意", "退群", "翻车",
)

# 负反馈：老大说"这不是外应 / 别拿我说事"时，本群安静一段时间
NEGATIVE_MARKERS = ("不是外应", "别拿我说事", "别扯我", "别用我", "别乱说")

PROBE_PROMPT = """你是呆猫的「顺风耳」，只负责听，不负责解卦。

下面是 QQ 群最近 {window} 分钟的消息（越往下越新）：
{transcript}

任务：最多找一条「外应」——某位群友随口说的、跟群里另一件还没落地的事结构同构的闲话。

硬判据（缺一不可）：
1. 异轨：说这话的人不是那件事的当事人，也没在回应那件事；
2. 同构：能写出一句「他那件事 X，所以这件事也会 X」，且不需要补充任何原文没有的事实；
3. 具体：至少有 2 个可对应的结构节点（"先接单→后又被退"算 2 个；"卡住""成了"只有 1 个，不算）；
4. 势能：能判断这股劲是趋成、趋败、还是僵住，以及快、慢、骤停。

绝对排除：私事、病痛、家人、吵架、金钱纠纷、宗教神明讨论、骂人，以及呆猫自己的发言。

只输出 JSON，不要解释、不要代码块：
{{"hit": true, "speaker": "群昵称", "quote": "原话不超过{q}字", "matter_owner": "那件事是谁的", "matter": "那件事不超过30字", "mapping": "一句话链路不超过40字", "energy": "趋成|趋败|僵住", "tempo": "快|慢|骤停", "strength": 0.0}}
没有合格的就输出 {{"hit": false}}。"""

COMPOSE_PROMPT = """你是呆猫，本名小苗。外表憨憨，心思直接，叫对方“老大”。

你刚在群里听见一句话，跟老大心里悬着的那件事是一个形状。
【听见的话】{speaker}：「{quote}」
【对应的事】{matter}（{matter_owner} 的）
【为什么是外应】{mapping}
【这股劲】{energy} · {tempo}

说给老大听。硬规则：
1. 必须点明这是外应、必须说出是谁说的：开头用「老大你看这个外应，{speaker}说的……」；
2. 一句话说完，不换行、不分点、不用 markdown，不超过 60 字；
3. 不复述对方的私事、不解释什么是外应；
4. 语气是“突然想起来插一句”，不是播报。

只输出这句话本身。"""


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象；抠不出返回空 dict。"""
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


class PluginSection(PluginConfigBase):
    __ui_label__ = "顺风耳"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用外应探针")
    dry_run: bool = Field(default=True, description="只判定不发言（先看日志确认准不准，再关掉）")
    window_minutes: int = Field(default=8, description="回看多少分钟的群聊")
    max_messages: int = Field(default=40, description="最多取多少条消息进提示词")
    probe_cooldown_seconds: int = Field(default=90, description="同一会话两次探针的最小间隔")
    say_cooldown_minutes: int = Field(default=25, description="同一会话两次开口的最小间隔")
    daily_cap: int = Field(default=6, description="每个会话每天最多主动说几次外应")
    min_strength: float = Field(default=0.7, description="判定强度阈值，低于此值不开口")
    max_quote_chars: int = Field(default=40, description="引用旁人原话的字数上限")
    judge_task: str = Field(default="replyer", description="判定使用的模型任务名")
    judge_model: str = Field(default="", description="判定使用的模型名")
    compose_task: str = Field(default="replyer", description="生成呆猫口吻的任务名")
    compose_model: str = Field(default="", description="生成呆猫口吻的模型名")
    group_allowlist: List[str] = Field(default_factory=list, description="只在这些群生效；留空=全部（黑名单除外）")
    group_blocklist: List[str] = Field(default_factory=list, description="这些群不启用")
    allow_private: bool = Field(default=False, description="私聊是否也听风（默认否）")
    bot_nicknames: List[str] = Field(default_factory=lambda: ["呆猫", "小苗"], description="呆猫自己的昵称")
    bot_user_ids: List[str] = Field(default_factory=list, description="呆猫自己的账号 ID")
    taboo_words: List[str] = Field(default_factory=list, description="命中就不探的消息词")
    protected_names: List[str] = Field(default_factory=list, description="命中就不探的名字")
    config_version: str = Field(default="0.1.0", description="配置版本")


class WaiyingConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class WaiyingPlugin(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = WaiyingConfig

    def __init__(self) -> None:
        super().__init__()
        self._inflight: dict[str, bool] = {}
        self._last_probe: dict[str, float] = {}
        self._last_say: dict[str, float] = {}
        self._daily: dict[str, tuple[str, int]] = {}
        self._muted_until: dict[str, float] = {}
        self._rng = random.SystemRandom()

    async def on_load(self) -> None:
        cfg = self._cfg()
        self.ctx.logger.info(
            "顺风耳已加载（dry_run=%s，窗口=%s 分钟，探针间隔=%ss，日配额=%s）",
            cfg.dry_run, cfg.window_minutes, cfg.probe_cooldown_seconds, cfg.daily_cap,
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str = "", config_data: dict | None = None, version: str = "") -> None:
        return None

    def _cfg(self) -> PluginSection:
        try:
            return self.config.plugin
        except Exception:  # noqa: BLE001
            return PluginSection()

    # ── 调试入口：/外应 立刻听一次风（绕过节流，方便标定）─────
    @Command("waiying", description="/外应 立刻按当前群聊听一次风（调试用）", pattern=r"^\s*(?:[@＠]\S+\s*)*/外应\s*$")
    async def handle_probe(self, stream_id: str = "", **kwargs: Any) -> tuple:
        cfg = self._cfg()
        if not stream_id:
            return True, "没有会话", True
        if self._inflight.get(stream_id):
            return True, "正在听", True
        self._inflight[stream_id] = True
        asyncio.create_task(self._probe(stream_id, "", "老大", cfg, force=True))
        return True, "听风中", True

    # ── 入口：每条消息只做规则粗筛，判定丢后台 ───────────────
    @HookHandler(
        hook="chat.receive.after_process",
        name="waiying_listen",
        description="听群里的异轨闲话（外应探针）",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=400,
        error_policy=ErrorPolicy.SKIP,
    )
    async def listen(self, **kwargs: Any) -> None:
        cfg = self._cfg()
        if not cfg.enabled:
            return None
        message: dict[str, Any] = kwargs.get("message", {}) or {}
        text = str(message.get("processed_plain_text") or message.get("plain_text") or "").strip()
        if not text:
            return None

        sid = str(message.get("session_id") or "")
        if not sid:
            return None

        # 负反馈：老大刚说"不是外应/别拿我说事"，本群安静一小时
        if any(mark in text for mark in NEGATIVE_MARKERS) and self._last_say.get(sid):
            if time.time() - self._last_say[sid] < 180:
                self._muted_until[sid] = time.time() + 3600
                self.ctx.logger.info("顺风耳收到负反馈，本群静默 1 小时: %s", sid)
                return None

        if not self._prefilter(message, text, sid, cfg):
            return None

        now = time.time()
        if self._inflight.get(sid):
            return None
        if now - self._last_probe.get(sid, 0.0) < cfg.probe_cooldown_seconds:
            return None
        if now < self._muted_until.get(sid, 0.0):
            return None
        if not self._daily_left(sid, cfg):
            return None

        self._inflight[sid] = True
        info = message.get("message_info") or {}
        user = info.get("user_info") or {}
        nickname = str(user.get("user_nickname") or user.get("user_cardname") or "某人")
        asyncio.create_task(self._probe(sid, text, nickname, cfg))
        return None

    def _prefilter(self, message: dict[str, Any], text: str, sid: str, cfg: PluginSection) -> bool:
        if message.get("is_command") or message.get("is_notify"):
            return False
        if message.get("is_emoji") or message.get("is_picture"):
            return False
        info = message.get("message_info") or {}
        user = info.get("user_info") or {}
        group = info.get("group_info") or {}
        uid = str(user.get("user_id") or "")
        nickname = str(user.get("user_nickname") or user.get("user_cardname") or "")

        if uid in cfg.bot_user_ids or nickname in cfg.bot_nicknames:
            return False

        is_group = bool(group.get("group_id"))
        if not is_group and not cfg.allow_private:
            return False
        gid = str(group.get("group_id") or "")
        if gid and cfg.group_blocklist and gid in cfg.group_blocklist:
            return False
        if gid and cfg.group_allowlist and gid not in cfg.group_allowlist:
            return False

        if any(w and w in text for w in cfg.taboo_words):
            return False
        if any(n and n in text for n in cfg.protected_names):
            return False
        if len(text) < 6:
            return False
        if text.strip("?？!！。.~～…、，, ") == "":
            return False

        has_cue = any(w in text for w in STRUCT_CUES)
        return bool(has_cue or len(text) >= 18)

    def _daily_left(self, sid: str, cfg: PluginSection) -> bool:
        today = time.strftime("%Y-%m-%d")
        day, count = self._daily.get(sid, (today, 0))
        if day != today:
            self._daily[sid] = (today, 0)
            return True
        return count < max(0, int(cfg.daily_cap))

    # ── 探针：查记录 → 判定 → 组句 → 发言 ─────────────────
    async def _probe(
        self, sid: str, trigger_text: str, trigger_name: str, cfg: PluginSection, force: bool = False
    ) -> None:
        try:
            transcript = await self._transcript(sid, cfg)
            # 触发消息可能还没落库（hook 早于入库），补一行，别让最关键的那句缺席
            if trigger_text and trigger_text[:20] not in transcript:
                extra = f"[此刻] {trigger_name}: {_clip(trigger_text, 90)}"
                transcript = f"{transcript}\n{extra}" if transcript else extra
            if not transcript:
                self.ctx.logger.debug("顺风耳探针: 群=%s 无可读记录，跳过", sid[-8:])
                return
            verdict = await self._judge(transcript, cfg)
            self.ctx.logger.debug(
                "顺风耳探针: 群=%s 行数=%s hit=%s strength=%s",
                sid[-8:], len(transcript.splitlines()), verdict.get("hit"), verdict.get("strength"),
            )
            if not verdict.get("hit"):
                return
            try:
                strength = float(verdict.get("strength") or 0.0)
            except (TypeError, ValueError):
                strength = 0.0
            if strength < float(cfg.min_strength):
                self.ctx.logger.info("顺风耳：命中但强度不足 %.2f < %.2f，弃", strength, cfg.min_strength)
                return

            now = time.time()
            if not force and now - self._last_say.get(sid, 0.0) < cfg.say_cooldown_minutes * 60:
                return

            line = await self._compose(verdict, cfg)
            if not line:
                return

            record = self._record(verdict, line, sid, trigger_text, cfg)

            if cfg.dry_run:
                self.ctx.logger.info("[dry_run] 顺风耳拟发言: %s", line)
                self._append_log(record)
                return

            await self.ctx.send.text(
                line,
                sid,
                sync_to_maisaka_history=True,
                maisaka_source_kind="guided_reply",
            )
            self._last_say[sid] = now
            day, count = self._daily.get(sid, (time.strftime("%Y-%m-%d"), 0))
            self._daily[sid] = (day, count + 1)
            self._append_log(record)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("顺风耳探针失败: %s", exc)
        finally:
            self._inflight[sid] = False
            self._last_probe[sid] = time.time()

    async def _transcript(self, sid: str, cfg: PluginSection) -> str:
        result = await self.ctx.call_capability(
            "message.get_recent",
            chat_id=sid,
            hours=max(1, int(cfg.window_minutes)) / 60.0,
            limit=int(cfg.max_messages),
            filter_mai=True,
            timeout_ms=8000,
        )
        messages = (result or {}).get("messages") or []
        if not isinstance(messages, list):
            return ""
        now = time.time()
        rows: list[tuple[float, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            if item.get("is_command") or item.get("is_notify"):
                continue
            text = str(item.get("processed_plain_text") or "").strip()
            if not text:
                continue
            info = item.get("message_info") or {}
            user = info.get("user_info") or {}
            name = str(user.get("user_nickname") or user.get("user_cardname") or "某人")
            if name in cfg.bot_nicknames:
                continue
            try:
                ts = float(item.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            rows.append((ts, f"{name}: {_clip(text, 90)}"))
        # 不依赖宿主返回顺序：按时间戳升序，保证提示词里"越往下越新"
        rows.sort(key=lambda pair: pair[0])
        lines = [
            f"[{max(0, int((now - ts) / 60))}分钟前] {body}" if ts else f"[未知时间] {body}"
            for ts, body in rows[-int(cfg.max_messages) :]
        ]
        return "\n".join(lines)

    async def _judge(self, transcript: str, cfg: PluginSection) -> dict[str, Any]:
        prompt = PROBE_PROMPT.format(
            window=cfg.window_minutes, transcript=transcript, q=cfg.max_quote_chars
        )
        try:
            result = await self.ctx.call_capability(
                "llm.generate",
                timeout_ms=45000,
                prompt=prompt,
                task_name=str(cfg.judge_task or "replyer"),
                model_name=str(cfg.judge_model or ""),
                temperature=0.1,
                max_tokens=400,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("顺风耳判定调用失败: %s", exc)
            return {}
        return _extract_json(str((result or {}).get("response") or ""))

    async def _compose(self, verdict: dict[str, Any], cfg: PluginSection) -> str:
        speaker = _clip(str(verdict.get("speaker") or "有人"), 12)
        quote = _clip(str(verdict.get("quote") or ""), int(cfg.max_quote_chars))
        matter = _clip(str(verdict.get("matter") or ""), 30)
        owner = _clip(str(verdict.get("matter_owner") or "老大"), 12)
        mapping = _clip(str(verdict.get("mapping") or ""), 40)
        energy = str(verdict.get("energy") or "")
        tempo = str(verdict.get("tempo") or "")
        if not quote or not mapping:
            return ""

        prompt = COMPOSE_PROMPT.format(
            speaker=speaker, quote=quote, matter=matter, matter_owner=owner,
            mapping=mapping, energy=energy, tempo=tempo,
        )
        try:
            result = await self.ctx.call_capability(
                "llm.generate",
                timeout_ms=45000,
                prompt=prompt,
                task_name=str(cfg.compose_task or "replyer"),
                model_name=str(cfg.compose_model or ""),
                temperature=0.8,
                max_tokens=200,
            )
            line = str((result or {}).get("response") or "").strip()
            line = " ".join(line.split())
            if line and "外应" in line:
                return _clip(line, 90)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("顺风耳组句失败，退回模板: %s", exc)

        # 模板兜底：至少在判定成功时说得出口
        tail = "，".join(x for x in (mapping, (energy + tempo).strip()) if x)
        return _clip(f"老大你看这个外应，{speaker}说的「{quote}」——{tail}", 90)

    def _record(
        self, verdict: dict[str, Any], line: str, sid: str, trigger: str, cfg: PluginSection
    ) -> dict[str, Any]:
        return {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": sid,
            "dry_run": bool(cfg.dry_run),
            "speaker": verdict.get("speaker"),
            "quote": verdict.get("quote"),
            "matter_owner": verdict.get("matter_owner"),
            "matter": verdict.get("matter"),
            "mapping": verdict.get("mapping"),
            "energy": verdict.get("energy"),
            "tempo": verdict.get("tempo"),
            "strength": verdict.get("strength"),
            "line": line,
            "trigger_text": _clip(trigger, 120),
        }

    def _append_log(self, record: dict[str, Any]) -> None:
        try:
            path: Path = self.ctx.paths.data_dir / "waiying.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass


def create_plugin() -> WaiyingPlugin:
    return WaiyingPlugin()
