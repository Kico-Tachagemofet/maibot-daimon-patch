"""Local memory lifetime, audience and deterministic profile views.

The paragraph remains the source of retrieval and profile visibility. Cold
archives retain their text and do not enter the vendor deletion/purge queue.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable

RECENT_SECONDS = 7 * 86400
LONG_KINDS = {"fact", "shared_experience"}
PROFILE_SECTIONS = {"identity_settings", "relationship_settings", "stable_facts",
                    "interaction_preferences", "recent_interactions", "uncertain_notes"}


def metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))


def memory_kind(row: dict[str, Any]) -> str:
    meta = metadata(row.get("metadata"))
    explicit = str(meta.get("memory_kind", "") or "").strip()
    if explicit:
        return explicit
    source = str(row.get("source", "") or meta.get("source_type", "") or "")
    if source.startswith(("person_fact", "web_import")) or row.get("is_permanent"):
        return "fact"
    if source.startswith("shared_experience"):
        return "shared_experience"
    if source.startswith("rule_note"):
        return "rule_note"
    return "recent_event"


def is_durable(row: dict[str, Any]) -> bool:
    meta = metadata(row.get("metadata"))
    return memory_kind(row) in LONG_KINDS or bool(meta.get("explicit_remember") or meta.get("manual_pinned"))


def retention_deadline(row: dict[str, Any]) -> float | None:
    meta = metadata(row.get("metadata"))
    if is_durable(row):
        return None
    explicit = number(meta.get("retention_until"))
    if explicit is not None:
        return explicit
    observed = (number(meta.get("occurred_at")) or number(row.get("event_time_end"))
                or number(row.get("created_at")))
    return observed + RECENT_SECONDS if observed is not None else None


def paragraph_active(row: dict[str, Any] | None, now: float | None = None,
                     include_rules: bool = False) -> bool:
    if not isinstance(row, dict) or row.get("is_deleted"):
        return False
    point = time.time() if now is None else float(now)
    meta = metadata(row.get("metadata"))
    if str(meta.get("retention_state", "active")) != "active":
        return False
    if meta.get("dedup_phase") == "pending":
        return False
    if memory_kind(row) == "rule_note" and not include_rules:
        return False
    change = metadata(meta.get("memory_change"))
    valid_to = number(change.get("valid_to"))
    if valid_to is not None and valid_to <= point:
        return False
    # Explicit vendor expirations retain their original semantics.
    expires = number(row.get("expires_at"))
    if expires is not None and expires <= point:
        return False
    deadline = retention_deadline(row)
    return deadline is None or deadline > point


def visibility_key(meta: dict[str, Any], source: str = "", chat_id: str = "") -> str:
    meta = metadata(meta)
    visibility = metadata(meta.get("visibility"))
    mode = str(visibility.get("mode", "") or "")
    chats = tokens(visibility.get("chat_ids"))
    if not mode:
        mode = "shared" if meta.get("scope_type") == "global" or source.startswith("web_import:") else "chat"
        chats = tokens(meta.get("chat_ids")) or tokens(meta.get("chat_id")) or tokens(chat_id)
        if not chats and source.startswith("chat_summary:"):
            chats = [source.split(":", 1)[1]]
    return json.dumps([mode, sorted(chats), str(visibility.get("owner_user_id", "") or "")],
                      ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=512)
def _chat_identity(chat_id: str) -> tuple[str, str]:
    """Resolve owner-private requests made by a caller that supplies only stream ID."""
    try:
        from src.common.paths import get_instance_root
        database = Path(get_instance_root()) / "data" / "MaiBot.db"
        if not database.exists():
            return "", "unknown"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute(
                "SELECT user_id, group_id FROM chat_sessions WHERE session_id=? ORDER BY last_active_timestamp DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return (str(row[0] or ""), str(row[1] or "")) if row else ("", "unknown")
    except (ImportError, OSError, sqlite3.Error):
        return "", "unknown"


def paragraph_visible(row: dict[str, Any] | None, chat_id: str = "",
                      shared_chat_ids: Iterable[str] = (), user_id: str = "",
                      group_id: str = "", now: float | None = None) -> bool:
    if not paragraph_active(row, now=now):
        return False
    if not chat_id:
        return True  # Local administration, not a chat request.
    meta = metadata(row.get("metadata"))
    visibility = metadata(meta.get("visibility"))
    mode = str(visibility.get("mode", "") or "")
    if mode == "owner_private":
        resolved_user, resolved_group = _chat_identity(chat_id)
        current_user = user_id or resolved_user
        current_group = resolved_group if resolved_group != "unknown" else group_id
        owner = str(visibility.get("owner_user_id", "") or "")
        return bool(owner and current_user == owner and not current_group)
    allowed = set(tokens(shared_chat_ids)) | {str(chat_id)}
    if group_id:
        allowed.add(str(group_id))
    if mode:
        return bool(set(tokens(visibility.get("chat_ids"))) & allowed)
    source = str(row.get("source", "") or "")
    if meta.get("scope_type") == "global" or source.startswith("web_import:"):
        return True
    chats = set(tokens(meta.get("chat_id"))) | set(tokens(meta.get("chat_ids")))
    if source.startswith("chat_summary:"):
        chats.add(source.split(":", 1)[1])
    return bool(chats & allowed)


def claim_visible(store: Any, claim: dict[str, Any], **audience: Any) -> bool:
    evidence = store.get_fact_evidence(str(claim.get("claim_id", "")))
    supports = [e for e in evidence if e.get("evidence_type") == "paragraph" and e.get("stance", "support") == "support"]
    if not supports:
        return not audience.get("chat_id") and claim.get("authority") == "manual"
    section = str(claim.get("profile_section", "stable_facts") or "stable_facts")
    for evidence_row in supports:
        paragraph = store.get_paragraph(evidence_row["evidence_id"])
        if not paragraph_visible(paragraph, **audience):
            continue
        if section not in {"recent_interactions", "uncertain_notes"} and memory_kind(paragraph) != "fact":
            continue
        return True
    return False


def _profile_people(row: dict[str, Any]) -> set[str]:
    meta = metadata(row.get("metadata"))
    explicit = tokens(meta.get("profile_person_ids"))
    if explicit:
        return set(explicit)
    if meta.get("person_id"):
        return {str(meta["person_id"])}
    # A multi-participant chat summary is retrieval material, not each participant's biography.
    people = tokens(meta.get("person_ids"))
    if len(people) == 1 and memory_kind(row) != "shared_experience":
        return set(people)
    return set()


def _relevance(text: str, context: str) -> int:
    def grams(value: str) -> set[str]:
        value = "".join(c for c in value.casefold() if c.isalnum())
        return {value[i:i+2] for i in range(max(0, len(value)-1))}
    return len(grams(text) & grams(context)) if context else 0


def build_profile_view(service: Any, person_id: str, chat_id: str = "", user_id: str = "",
                       group_id: str = "", context_text: str = "", top_k: int = 12,
                       shared_chat_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Use current visible sources; the prose is the stored natural memory text."""
    from .profile_text import build_structured_profile_text
    store = service.metadata_store
    identity = store.get_person_identity(person_id) or {}
    name = str(identity.get("canonical_name", "") or person_id)
    aliases: list[str] = []
    if not chat_id:
        aliases, name, _ = service.get_person_aliases(person_id)
    audience = {"chat_id": chat_id, "user_id": user_id, "group_id": group_id,
                "shared_chat_ids": shared_chat_ids}
    buckets: dict[str, list[str]] = {section: [] for section in PROFILE_SECTIONS}
    claims = [c for c in store.list_person_profile_fact_claims(person_id, effective_at=time.time(), limit=512)
              if claim_visible(store, c, **audience)]
    def priority(claim: dict[str, Any]) -> float:
        values = [0.0]
        for item in store.get_fact_evidence(claim["claim_id"]):
            if item.get("evidence_type") != "paragraph":
                continue
            paragraph = store.get_paragraph(item["evidence_id"])
            if paragraph and paragraph_visible(paragraph, **audience):
                meta = metadata(paragraph.get("metadata"))
                values.append(number(meta.get("profile_priority")) or (1.0 if meta.get("explicit_remember") or meta.get("manual_pinned") else 0.0))
        return max(values)
    claims.sort(key=lambda c: (priority(c), c.get("authority") == "manual",
        _relevance(str(c.get("value_text", "")), context_text),
        number(c.get("last_confirmed_at")) or number(c.get("updated_at")) or number(c.get("created_at")) or 0), reverse=True)
    evidence_ids: list[str] = []
    for c in claims:
        section = str(c.get("profile_section", "stable_facts"))
        if section not in buckets:
            section = "stable_facts"
        text = str(c.get("value_text", "") or "").strip()
        if text and text not in buckets[section]:
            buckets[section].append(text)
        evidence_ids.extend(e["evidence_id"] for e in store.get_fact_evidence(c["claim_id"])
            if e.get("evidence_type") == "paragraph" and paragraph_visible(store.get_paragraph(e["evidence_id"]), **audience))
    paragraphs = store.query("SELECT * FROM paragraphs WHERE COALESCE(is_deleted,0)=0 ORDER BY created_at DESC")
    paragraphs = [p for p in paragraphs if person_id in _profile_people(p) and paragraph_visible(p, **audience)]
    paragraphs.sort(key=lambda p: (_relevance(str(p.get("content", "")), context_text),
        number(metadata(p.get("metadata")).get("occurred_at")) or number(p.get("created_at")) or 0), reverse=True)
    for paragraph in paragraphs:
        meta = metadata(paragraph.get("metadata"))
        kind = memory_kind(paragraph)
        if kind == "fact":
            continue  # Current active claims project facts, not stale paragraph wording.
        section = str(meta.get("profile_section", "") or "")
        if kind == "recent_event":
            section = section if section in {"recent_interactions", "uncertain_notes"} else "recent_interactions"
        elif kind == "shared_experience" and section not in PROFILE_SECTIONS:
            continue  # Stories remain available through topic retrieval.
        if section not in buckets:
            continue
        text = str(paragraph.get("content", "") or "").strip()
        if text and text not in buckets[section]:
            buckets[section].append(text)
            evidence_ids.append(str(paragraph["hash"]))
    text = build_structured_profile_text(person_id=person_id, primary_name=name, aliases=aliases,
        **{section: values for section, values in buckets.items()})
    return {"success": True, "person_id": person_id, "person_name": name,
        "profile_text": text, "aliases": aliases, "relation_edges": [], "vector_evidence": [],
        "evidence_ids": list(dict.fromkeys(evidence_ids)), "fact_claim_ids": [c["claim_id"] for c in claims],
        "from_cache": False, "profile_source": "retained_memory_view",
        "uncertain_fact_count": len(buckets["uncertain_notes"]),
        "uncertain_candidates": [{"text": x} for x in buckets["uncertain_notes"][:2]],
        "audience_chat_id": chat_id}


def synchronize_retention(service: Any, now: float | None = None) -> dict[str, int]:
    """Archive elapsed recent sources and protect relations from all effective sources."""
    from .memory_lifecycle_policy import RelationLifecycleEvent
    point = time.time() if now is None else float(now)
    store = service.metadata_store
    paragraphs = store.query("SELECT * FROM paragraphs WHERE COALESCE(is_deleted,0)=0")
    changed: list[dict[str, Any]] = []
    for row in paragraphs:
        meta = metadata(row.get("metadata"))
        deadline = retention_deadline(row)
        if meta.get("retention_state", "active") == "active" and memory_kind(row) not in LONG_KINDS and deadline is not None and deadline <= point:
            meta.update(retention_state="archived", retention_archived_at=point)
            store.update_paragraph_metadata(row["hash"], meta, merge=False)
            row["metadata"] = meta
            changed.append(row)
    for source in {str(p.get("source", "")) for p in changed if p.get("source")}:
        store.enqueue_episode_source_rebuild(source, reason="recent_memory_archived")
    people = set()
    for row in changed:
        people.update(_profile_people(row))
    for person_id in people:
        store.enqueue_person_profile_refresh(person_id, reason="recent_memory_archived")
    relations = store.query("SELECT * FROM relations")
    supporting = store.get_paragraphs_by_relation_hashes([r["hash"] for r in relations]) if relations else {}
    protected = 0
    for relation in relations:
        meta = metadata(relation.get("metadata"))
        marker = metadata(meta.get("local_retention_protection"))
        sources = list(supporting.get(relation["hash"], []))
        source_hash = str(relation.get("source_paragraph", "") or "")
        if source_hash and not any(p.get("hash") == source_hash for p in sources):
            source = store.get_paragraph(source_hash)
            if source:
                sources.append(source)
        sources = [p for p in sources if paragraph_active(p, now=point)
                   and store.fact_relation_has_support(relation["hash"], p)]
        if relation.get("inactive_reason") in {"manual_forget", "manual_freeze"}:
            sources = []
        if not sources and not marker:
            continue
        if not marker:
            marker = {"previous_pinned": bool(relation.get("is_pinned")),
                      "previous_protected_until": number(relation.get("protected_until")) or 0.0}
        long = any(is_durable(p) for p in sources)
        until = max([number(marker.get("previous_protected_until")) or 0.0]
                    + [retention_deadline(p) or 0.0 for p in sources])
        pin = bool(marker.get("previous_pinned")) or long
        store.update_relations_protection([relation["hash"]], protected_until=until, is_pinned=pin, _retention_managed=True)
        if sources:
            meta["local_retention_protection"] = marker
            protected += 1
            if relation.get("is_inactive") and relation.get("inactive_reason") == "decay":
                type(service).apply_relation_lifecycle_event(service, [relation["hash"]], event=RelationLifecycleEvent.EVIDENCE)
        else:
            meta.pop("local_retention_protection", None)
        with store.transaction(immediate=True) as connection:
            connection.execute("UPDATE relations SET metadata=? WHERE hash=?",
                (json.dumps(meta, ensure_ascii=False), relation["hash"]))
    return {"archived": len(changed), "protected_relations": protected}
