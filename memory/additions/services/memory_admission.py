"""Typed memory admission with local conversation context.

Only this module's metadata carries classification/provenance. The memory text is
an ordinary sentence describing a person, an interaction, or an event.
"""
from __future__ import annotations

from datetime import datetime, time as daytime, timezone, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import json
import re
import time
import unicodedata

KINDS = frozenset({"fact", "shared_experience", "recent_event", "rule_note"})
PROFILE_SECTIONS = frozenset({
    "identity_settings", "relationship_settings", "stable_facts", "interaction_preferences",
})
LOCAL_TZ = timezone(timedelta(hours=8))
def _default_context_path() -> Path:
    from src.common.paths import get_instance_root
    return Path(get_instance_root()) / "config" / "memory_context.json"

PSEUDO_USERS = frozenset({"all", "notice", "self"})


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def session_id_for_message(message: Any) -> str:
    session = getattr(message, "session", None)
    return str(getattr(message, "session_id", "") or getattr(session, "session_id", "") or "").strip()


def _identity(message: Any) -> tuple[str, str]:
    user = getattr(getattr(message, "message_info", None), "user_info", None)
    uid = str(getattr(user, "user_id", "") or getattr(message, "user_id", "") or "").strip()
    name = str(getattr(user, "user_cardname", "") or getattr(user, "user_nickname", "")
               or getattr(message, "user_cardname", "") or getattr(message, "user_nickname", "") or uid).strip()
    return uid, name


def message_timestamp(message: Any, fallback: float | None = None) -> float:
    value = getattr(message, "timestamp", None)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=LOCAL_TZ).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except ValueError:
            pass
    return float(fallback if fallback is not None else time.time())


def _drop_balanced_prefix(text: str, opener: str, closer: str) -> str:
    depth = 0
    for index, char in enumerate(text):
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[index + 1:].lstrip()
    return ""


def authored_text(text: Any) -> str:
    """Return the sender's own remainder after leading reply/forward blocks."""
    value = str(text or "").strip()
    while value.startswith(("[回复了", "[回复消息]", "[回复了一条消息")):
        value = _drop_balanced_prefix(value, "[", "]")
    if value.startswith("【合并转发消息"):
        value = _drop_balanced_prefix(value, "【", "】")
    return value.strip()


def _merge_card(base: Dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key, value in incoming.items():
        if key in {"names", "terms"} and isinstance(value, dict):
            base[key] = {**base.get(key, {}), **value}
        elif key in {"context", "bots", "owner_user_ids", "private_terms"}:
            base[key] = list(dict.fromkeys(_strings(base.get(key)) + _strings(value)))
        else:
            base[key] = value


def load_context(*, session_id: str = "", group_id: str = "", path: Path | None = None) -> Dict[str, Any]:
    card: Dict[str, Any] = {"recent_days": 7, "owner_user_ids": [],
                            "context": [], "names": {}, "terms": {}, "bots": [], "private_terms": []}
    config_path = path or _default_context_path()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return card
    if not isinstance(data, dict):
        raise ValueError("memory_context.json must contain a JSON object")
    for key in ("recent_days", "owner_user_ids", "private_terms"):
        if key in data:
            card[key] = data[key]
    defaults = data.get("defaults", {})
    if isinstance(defaults, dict):
        _merge_card(card, defaults)
    groups = data.get("groups", {})
    if isinstance(groups, dict):
        for scope in dict.fromkeys([str(group_id), str(session_id)]):
            if scope and isinstance(groups.get(scope), dict):
                _merge_card(card, groups[scope])
    card["recent_days"] = max(1, int(card.get("recent_days", 7)))
    return card


def canonical_name(user_id: str, fallback: str, card: Mapping[str, Any]) -> str:
    names = card.get("names", {})
    entry = names.get(user_id) if isinstance(names, dict) else None
    if isinstance(entry, str):
        return entry.strip() or fallback
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("canonical_name") or fallback).strip()
    return fallback


def source_rows(evidence: Any, reply_message: Any, card: Mapping[str, Any],
                *, target_user_id: str, target_name: str) -> List[Dict[str, Any]]:
    """Keep actual message IDs and speaker identity next to authored text."""
    rows: Dict[str, Dict[str, Any]] = {}
    target_ids = {str(getattr(x, "message_id", "") or "") for x in evidence.target_messages}
    messages = [*evidence.context_messages, *evidence.target_messages, reply_message]
    reply_id = str(getattr(reply_message, "message_id", "") or "")
    for item in messages:
        mid = str(getattr(item, "message_id", "") or "").strip()
        uid, name = _identity(item)
        body = authored_text(getattr(item, "processed_plain_text", ""))
        if not mid or not body:
            continue
        if mid == reply_id and item is reply_message:
            role = "bot_reply"
        elif uid in _strings(card.get("bots")) or uid in PSEUDO_USERS:
            role = "other_bot_or_system"
        else:
            role = "target" if mid in target_ids and uid == target_user_id else "participant"
        if uid == target_user_id:
            name = canonical_name(uid, target_name, card)
        else:
            name = canonical_name(uid, name, card)
        rows[mid] = {"message_id": mid, "user_id": uid, "name": name,
                     "role": role, "time": message_timestamp(item), "text": body}
    return sorted(rows.values(), key=lambda row: (row["time"], row["message_id"]))


def repeated_lines(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        if row["role"] in {"bot_reply", "other_bot_or_system"}:
            continue
        key = " ".join(unicodedata.normalize("NFKC", str(row["text"])).split())
        if len(key) >= 12:
            groups.setdefault(key, []).append(row)
    result = []
    for text, items in groups.items():
        if len({row["user_id"] for row in items}) >= 2 and max(row["time"] for row in items) - min(row["time"] for row in items) <= 180:
            result.append({"text": text, "message_ids": [row["message_id"] for row in items],
                           "speakers": list(dict.fromkeys(row["name"] for row in items))})
    return result


def build_prompt(*, person_name: str, target_user_id: str,
                 rows: Sequence[Mapping[str, Any]], card: Mapping[str, Any]) -> str:
    context = {key: card.get(key) for key in ("context", "names", "terms") if card.get(key)}
    today = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    return f"""把这段群聊整理成少量以后能自然接话的记忆。先辨认发言人、话中对象和互动场景，再写简洁、具体的中文句子。有趣的昵称、玩笑、复读和共同经历都可以留下。

当前时间（UTC+8）：{today}
本次主要发言人：{person_name}；账号：{target_user_id}
本群已有语境：{json.dumps(context, ensure_ascii=False)}
消息：{json.dumps(list(rows), ensure_ascii=False)}
相邻复读线索：{json.dumps(repeated_lines(rows), ensure_ascii=False)}

选择 memory_kind：
- fact：发言者明确说过或确认过、以后仍有用的人物事实、称呼关系和互动偏好。
- shared_experience：发生在对话里的相处经历、共同梗、约定。记录谁说了什么，约定和完成各用对应动词。
- recent_event：最近的近况、当次提问、临时安排、感受和未定计划。默认保存7天；明确未来日期放到 keep_until。
- rule_note：机器人规则、模型配置、工具调试和运行安排。

写法：
1. memory_text 用实际名字或清楚的称呼。正文就是日常记忆句子，分类、证据和审阅意见写在字段里。
2. fact 的主体是本次发言人；如他在讲呆猫的命盘、别人的偏好或网站事件，选相应事件／经历类别并写清对象。
3. 每项 evidence 选真实消息ID和该条消息中的原句。target 是本人发言；participant 是别人；bot_reply 是猫已经发出的回复。人物事实以 target 的话为依据，猫的回复可用于记录互动经历。
4. 发言里的回复引用已经单独剥离，text 是此人自己说的部分。转发或回复机器人的解读，作为分享或讨论记录；喜欢故事中的场景与本人日常偏好分别表达。
5. 连续多人复读相同长句时，把整段和接梗记为 shared_experience。昵称、专业称谓和互动玩法按群语境理解，使用群友原本的叫法。
6. profile_section 为 identity_settings / relationship_settings / stable_facts / interaction_preferences，仅 fact 使用。短期情绪和当次目标选 recent_event。

只输出JSON数组，最多5项。每项字段：memory_kind、memory_text、subject_user_id、profile_section、evidence（message_id、quote的数组）、keep_until（明确未来事项的ISO日期或null）、explicit_remember（本人明确要求记住时true）。没有新内容就输出[]。"""


def parse_memories(raw: str) -> List[Dict[str, Any]]:
    from json_repair import repair_json
    try:
        repaired = repair_json(str(raw or ""))
        values = json.loads(repaired) if isinstance(repaired, str) else repaired
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)][:5]


def _keep_until(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)):
        return float(value)
    try:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if len(text) == 10:
            parsed = datetime.combine(parsed.date(), daytime.max, tzinfo=LOCAL_TZ)
        elif parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.timestamp()
    except ValueError:
        return None


def prepare_memory(item: Mapping[str, Any], *, rows: Sequence[Mapping[str, Any]],
                   card: Mapping[str, Any], target_user_id: str,
                   person_id: str, person_name: str, session_id: str,
                   group_id: str, platform: str) -> Dict[str, Any] | None:
    kind = str(item.get("memory_kind", "")).strip()
    text = str(item.get("memory_text", "")).strip()
    if kind not in KINDS or len(text) < 2:
        return None
    by_id = {str(row["message_id"]): row for row in rows}
    evidence = []
    for value in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
        if not isinstance(value, dict):
            continue
        mid = str(value.get("message_id", "") or "").strip()
        quote = str(value.get("quote", "") or "").strip()
        row = by_id.get(mid)
        if row is not None and quote and quote in row["text"]:
            evidence.append({"message_id": mid, "quote": quote, "user_id": row["user_id"]})
    if not evidence:
        return None
    cited = [by_id[e["message_id"]] for e in evidence]
    subject = str(item.get("subject_user_id", "") or "").strip()
    if kind == "fact":
        # Exact speaker and authored-quote checks are mechanical; semantic
        # classification remains the extractor's job, preserving natural prose.
        if subject != target_user_id or not any(row["role"] == "target" and row["user_id"] == subject for row in cited):
            return None
        evidence = [e for e in evidence if by_id[e["message_id"]]["role"] == "target"
                    and e["user_id"] == target_user_id]
        cited = [by_id[e["message_id"]] for e in evidence]
        for repeat in repeated_lines(rows):
            if set(repeat["message_ids"]) & {e["message_id"] for e in evidence}:
                kind = "shared_experience"
                names = "、".join(repeat["speakers"])
                text = f'{names}接连说了“{repeat["text"]}”，一起玩过这个复读梗。'
                evidence = [{"message_id": mid, "quote": by_id[mid]["text"], "user_id": by_id[mid]["user_id"]}
                            for mid in repeat["message_ids"]]
                cited = [by_id[e["message_id"]] for e in evidence]
                replies = [row for row in rows if row["role"] == "bot_reply"
                           and 0 <= float(row["time"]) - max(float(x["time"]) for x in cited) <= 180]
                if replies:
                    answer = replies[-1]
                    if len(str(answer["text"])) <= 240:
                        text += f'呆猫接着回：“{answer["text"]}”'
                        evidence.append({"message_id": answer["message_id"], "quote": answer["text"],
                                         "user_id": answer["user_id"]})
                        cited.append(answer)
                break
    occurred_at = max(float(row["time"]) for row in cited)
    long_term = kind in {"fact", "shared_experience"}
    retention_until = None if long_term else occurred_at + int(card.get("recent_days", 7)) * 86400
    future = _keep_until(item.get("keep_until"))
    if retention_until is not None and future is not None:
        retention_until = max(retention_until, future)
    owner_ids = _strings(card.get("owner_user_ids"))
    private_terms = _strings(card.get("private_terms"))
    owner_private = bool(owner_ids) and ((not group_id and target_user_id in owner_ids) or any(term.casefold() in text.casefold() for term in private_terms))
    visibility: Dict[str, Any] = {"mode": "chat", "chat_ids": [session_id]}
    if owner_private:
        visibility = {"mode": "owner_private", "owner_user_id": target_user_id if target_user_id in owner_ids else owner_ids[0], "chat_ids": [session_id]}
    configured = card.get("visibility")
    if not owner_private and isinstance(configured, dict) and configured.get("mode") == "shared":
        visibility = {"mode": "shared", "chat_ids": _strings(configured.get("chat_ids"))}
    explicit = bool(item.get("explicit_remember")) and any(
        row["role"] == "target" and re.search(r"记住|记下|别忘|以后见|以后叫", row["text"])
        for row in cited
    )
    manual_pinned = bool(item.get("manual_pinned", False))
    if explicit or manual_pinned:
        retention_until = None
    memory_meta: Dict[str, Any] = {
        "memory_kind": kind, "retention_state": "active", "retention_until": retention_until,
        "visibility": visibility, "occurred_at": occurred_at, "last_confirmed_at": occurred_at,
        "explicit_remember": bool(explicit), "manual_pinned": manual_pinned,
        "person_id": person_id, "person_name": person_name, "subject_user_id": target_user_id,
        "writeback_source": "memory_flow_service", "evidence_source": "user_supported" if kind == "fact" else "conversation_supported",
        "evidence_message_ids": list(dict.fromkeys(e["message_id"] for e in evidence)),
        "evidence_quotes": evidence, "platform": platform, "admission_version": 1,
    }
    if kind == "fact":
        section = str(item.get("profile_section", "stable_facts"))
        if section not in PROFILE_SECTIONS:
            section = "stable_facts"
        memory_meta["fact_claim"] = {"authority": "direct_user", "stability": "stable",
                                     "profile_section": section, "valid_from": occurred_at}
    identity = json.dumps([kind, person_id, session_id, sorted(memory_meta["evidence_message_ids"]), text],
                          ensure_ascii=False, separators=(",", ":"))
    return {"external_id": f'memory_admission:{kind}:{sha256(identity.encode()).hexdigest()}',
            "source_type": "person_fact" if kind == "fact" else kind,
            "text": text, "chat_id": session_id, "person_ids": [person_id],
            "participants": list(dict.fromkeys(row["name"] for row in cited)),
            "timestamp": occurred_at, "tags": [kind], "metadata": memory_meta,
            "respect_filter": True, "user_id": target_user_id, "group_id": group_id}
