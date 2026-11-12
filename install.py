"""Preview or install the Daimon patch into an initialized MaiBot 1.3.19 workspace.

Use MaiBot's Python environment. Stop the target instance before applying or
restoring. Preview is read-only; installation preserves existing plugin settings
and, after the first install, the user's personality configuration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
from typing import Any

import tomlkit

PACKAGE = Path(__file__).resolve().parent
PLUGIN_NAMES = (
    "daimon_pulse", "user_tarot-card-plugin", "kico_meihua",
    "kico_deity_lore", "kico_astragaloi", "kico_waiying",
)
STATE_PATH = Path("data/daimon-patch/install-state.json")


def _within(root: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root):
        raise ValueError(f"Path must stay inside {root}: {relative}")
    return target


def _read(path: Path) -> bytes | None:
    if path.exists() and not path.is_file():
        raise ValueError(f"Expected a file: {path}")
    return path.read_bytes() if path.exists() else None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _memory_module():
    spec = importlib.util.spec_from_file_location("daimon_memory_installer", PACKAGE / "memory/install.py")
    if spec is None or spec.loader is None:
        raise ValueError("Memory installer is missing")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _default_source_root() -> Path:
    return Path(importlib.metadata.distribution("maibot").locate_file("src")).resolve()


def _package_files(folder: Path):
    if not folder.is_dir():
        raise ValueError(f"Package directory is missing: {folder}")
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            yield relative, path.read_bytes()


def _personality(original: bytes, preset: dict[str, Any]) -> bytes:
    document = tomlkit.parse(original.decode("utf-8-sig"))
    name = str(preset.get("name") or "呆猫")
    values = preset.get("personality")
    if not isinstance(values, dict) or not values:
        raise ValueError("Public personality preset is empty")
    if "personality" not in document:
        document["personality"] = tomlkit.table()
    personality = document["personality"]
    profiles = personality.get("profiles")
    if profiles is None:
        profiles = tomlkit.aot()
        personality["profiles"] = profiles
        profiles = personality["profiles"]
    matches = [profile for profile in profiles if profile.get("name") == name]
    if len(matches) > 1:
        raise ValueError(f"Multiple personality profiles are named {name}; select one before installation")
    if matches:
        profile = matches[0]
    else:
        profile = tomlkit.table()
        profile["name"] = name
        profiles.append(profile)
        profile = profiles[-1]
    for key, value in values.items():
        personality[key] = value
        profile[key] = value
    personality["selected_profile"] = name
    return tomlkit.dumps(document).encode("utf-8")


def build_plan(maibot_root: Path | str, source_root: Path | str | None = None,
               owner_id: str = "", bot_id: str = "") -> dict[str, Any]:
    root = Path(maibot_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("--maibot-root must be an existing initialized MaiBot workspace")
    bot_config = _within(root, "config/bot_config.toml")
    config_bytes = _read(bot_config)
    if config_bytes is None:
        raise ValueError("Initialize MaiBot first; config/bot_config.toml is missing")
    source = Path(source_root).expanduser().resolve() if source_root else _default_source_root()
    memory = _memory_module()
    memory_changes = memory.plan_install(source)
    state_target = _within(root, STATE_PATH)
    state_bytes = _read(state_target)
    state = json.loads(state_bytes.decode("utf-8")) if state_bytes else None
    first_install = state is None
    if state is not None and state.get("maibot_root") != str(root):
        raise ValueError("Previous install state belongs to a different MaiBot workspace")
    prior_after: dict[str, bytes] = {}
    tracked_files: dict[str, str] = {}
    if state is not None:
        tracked_files = dict(state.get("tracked_files", {}))
        for target, snapshot in tracked_files.items():
            prior_after[target] = Path(snapshot).read_bytes()
    operation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = _within(root, Path("tools/daimon-patch/backups") / operation)
    receipt_path = backup / "receipt.json"
    changes: list[dict[str, Any]] = []
    preserved: list[str] = []

    def add(target: Path, after: bytes, component: str, before: bytes | None = None,
            supplied_before: bool = False):
        original = before if supplied_before else _read(target)
        if original == after:
            return
        if str(target) in prior_after and original != prior_after[str(target)]:
            raise ValueError(f"Installed file was edited since the prior install: {target}")
        changes.append({"target": target, "before": original, "after": after, "component": component})

    for item in memory_changes:
        add(item["target"], item["after"], "memory", item["before"], True)
    for name in PLUGIN_NAMES:
        folder = PACKAGE / "plugins" / name
        if not (folder / "plugin.py").is_file() or not (folder / "_manifest.json").is_file():
            raise ValueError(f"Incomplete plugin in package: {name}")
        for relative, content in _package_files(folder):
            if relative.name == "config.toml":
                continue
            add(_within(root, Path("plugins") / name / relative), content, "plugins")
        example = folder / "config.example.toml"
        target = _within(root, Path("plugins") / name / "config.toml")
        if target.exists():
            preserved.append(str(target))
        elif example.exists():
            document = tomlkit.parse(example.read_text(encoding="utf-8"))
            if name == "kico_waiying" and bot_id:
                document["plugin"]["bot_user_ids"] = [bot_id]
            add(target, tomlkit.dumps(document).encode("utf-8"), "plugin_settings")
    for required in ("tarot-meanings/card_meanings.py", "deity-cards/cards.json",
                     "deity-cards/catalog.json", "astragaloi/asia_minor_56.json"):
        if not (PACKAGE / "resources" / required).is_file():
            raise ValueError(f"Required resource is missing: {required}")
    for relative, content in _package_files(PACKAGE / "resources"):
        add(_within(root, Path("data/daimon-resources") / relative), content, "resources")
    template = PACKAGE / "memory/memory_context.example.json"
    context = _within(root, "config/memory_context.json")
    if context.exists():
        preserved.append(str(context))
    else:
        payload = json.loads(template.read_text(encoding="utf-8"))
        if owner_id:
            payload["owner_user_ids"] = [owner_id]
        if bot_id:
            payload.setdefault("defaults", {})["bots"] = [bot_id]
        add(context, _json_bytes(payload), "memory_settings")
    if first_install:
        preset = tomllib.loads((PACKAGE / "presets/personality.toml").read_text(encoding="utf-8"))
        add(bot_config, _personality(config_bytes, preset), "personality")
    else:
        preserved.append(str(bot_config))
    if changes:
        for index, item in enumerate(changes):
            tracked_files[str(item["target"])] = str(backup / "after" / f"{index:04d}")
        new_state = {"format": 1, "maibot_root": str(root), "source_root": str(source),
                     "maibot_version": "1.3.19", "receipt": str(receipt_path),
                     "tracked_files": tracked_files}
        add(state_target, _json_bytes(new_state), "install_state")
    return {"maibot_root": root, "source_root": source, "first_install": first_install,
            "changes": changes, "preserved": preserved, "backup": backup, "receipt": receipt_path}


def preview(plan: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "preview", "maibot_root": str(plan["maibot_root"]),
            "source_root": str(plan["source_root"]), "maibot_version": "1.3.19",
            "first_install": plan["first_install"], "changed_files": len(plan["changes"]),
            "memory_policy": {"recent_days": 7, "long_term": ["fact", "shared_experience"],
                "database_migration": False,
                "notice": "安装默认启用7天近期记忆、长期人物事实和共同经历。安装本身不打开或迁移聊天库；首次启动后，现有未分类摘要会按兼容近期规则处理。"},
            "files": [{"path": str(item["target"]), "component": item["component"],
                       "action": "replace" if item["before"] is not None else "create"}
                      for item in plan["changes"]], "preserved": plan["preserved"]}


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".daimon-install-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _put(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _write(path, content)


def apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    result = preview(plan)
    result["mode"] = "apply"
    if not plan["changes"]:
        return {**result, "receipt": None}
    backup = plan["backup"]
    backup.mkdir(parents=True, exist_ok=False)
    entries = []
    for index, item in enumerate(plan["changes"]):
        if _read(item["target"]) != item["before"]:
            raise ValueError(f"Target changed since preview: {item['target']}")
        before_name = f"before/{index:04d}" if item["before"] is not None else None
        after_name = f"after/{index:04d}"
        if before_name:
            _write(backup / before_name, item["before"])
        _write(backup / after_name, item["after"])
        entries.append({"target": str(item["target"]), "component": item["component"],
                        "before": before_name, "after": after_name})
    receipt = {"format": 1, "maibot_root": str(plan["maibot_root"]),
               "source_root": str(plan["source_root"]), "status": "prepared", "files": entries}
    _write(plan["receipt"], _json_bytes(receipt))
    written = []
    try:
        for item in plan["changes"]:
            if _read(item["target"]) != item["before"]:
                raise ValueError(f"Target changed during installation: {item['target']}")
            _write(item["target"], item["after"])
            written.append(item)
        receipt["status"] = "applied"
        _write(plan["receipt"], _json_bytes(receipt))
    except BaseException:
        unresolved = []
        for item in reversed(written):
            try:
                if _read(item["target"]) != item["after"]:
                    unresolved.append(str(item["target"]))
                    continue
                _put(item["target"], item["before"])
            except OSError:
                unresolved.append(str(item["target"]))
        receipt.update(status="rollback_incomplete" if unresolved else "rolled_back", unresolved=unresolved)
        _write(plan["receipt"], _json_bytes(receipt))
        raise RuntimeError(f"Installation did not complete; {receipt['status']}. Receipt: {plan['receipt']}")
    return {**result, "receipt": str(plan["receipt"])}


def restore(maibot_root: Path | str, receipt_path: Path | str, apply: bool = False) -> dict[str, Any]:
    root = Path(maibot_root).expanduser().resolve()
    path = Path(receipt_path).expanduser().resolve()
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("format") != 1 or Path(receipt["maibot_root"]).resolve() != root:
        raise ValueError("Receipt does not belong to this --maibot-root")
    source = Path(receipt["source_root"]).resolve()
    changes = []
    conflicts = []
    for entry in receipt["files"]:
        target = Path(entry["target"]).resolve()
        if not (target.is_relative_to(root) or target.is_relative_to(source)):
            raise ValueError(f"Receipt target is outside its workspace and source roots: {target}")
        before = _within(path.parent, entry["before"]).read_bytes() if entry["before"] else None
        after = _within(path.parent, entry["after"]).read_bytes()
        current = _read(target)
        if current == before:
            continue
        if current != after:
            conflicts.append(str(target))
            continue
        changes.append({"target": target, "before": before, "after": after})
    if conflicts:
        raise ValueError("Restore would replace edits made after installation: " + "; ".join(conflicts))
    result = {"mode": "restore" if apply else "restore-preview", "changed_files": len(changes),
              "files": [str(item["target"]) for item in changes], "receipt": str(path)}
    if not apply:
        return result
    restored = []
    try:
        for item in reversed(changes):
            if _read(item["target"]) != item["after"]:
                raise ValueError(f"Target changed during restore: {item['target']}")
            _put(item["target"], item["before"])
            restored.append(item)
        receipt["status"] = "restored"
        _write(path, _json_bytes(receipt))
    except BaseException:
        unresolved = []
        for item in reversed(restored):
            try:
                if _read(item["target"]) != item["before"]:
                    unresolved.append(str(item["target"]))
                    continue
                _write(item["target"], item["after"])
            except OSError:
                unresolved.append(str(item["target"]))
        receipt.update(status="restore_failed", unresolved=unresolved)
        _write(path, _json_bytes(receipt))
        raise RuntimeError(f"Restore did not complete; see receipt: {path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maibot-root", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--owner-id", default="")
    parser.add_argument("--bot-id", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", type=Path, metavar="RECEIPT")
    args = parser.parse_args()
    try:
        if args.restore:
            result = restore(args.maibot_root, args.restore, args.apply)
        else:
            plan = build_plan(args.maibot_root, args.source_root, args.owner_id, args.bot_id)
            result = apply_plan(plan) if args.apply else preview(plan)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, RuntimeError, importlib.metadata.PackageNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
