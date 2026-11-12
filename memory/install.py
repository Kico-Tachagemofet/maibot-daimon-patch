"""Install the memory component into an existing MaiBot 1.3.19 src directory.

Preview is the default. Applying changes backs up original bytes, then updates
only the declared source contexts. Run this while the target MaiBot is stopped.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from email.parser import Parser
import json
import os
from pathlib import Path
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
SUPPORTED_VERSION = "1.3.19"


def _target(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root):
        raise ValueError(f"Source path must stay inside --source-root: {relative}")
    return candidate


def _version(root: Path) -> str:
    for metadata in root.parent.glob("maibot-*.dist-info/METADATA"):
        parsed = Parser().parsestr(metadata.read_text(encoding="utf-8"))
        if parsed.get("Name", "").lower().replace("_", "-") == "maibot":
            version = parsed.get("Version", "")
            if version != SUPPORTED_VERSION:
                raise ValueError(f"This patch supports MaiBot {SUPPORTED_VERSION}; found {version}")
            return version
    raise ValueError("Could not find adjacent maibot-*.dist-info/METADATA. Pass the installed MaiBot src directory.")


def _patched(source: str, operation: dict[str, Any]) -> str:
    result = source
    for index, hunk in enumerate(operation["hunks"], 1):
        old, new = hunk["old"], hunk["new"]
        if result.count(new) == 1:
            continue
        count = result.count(old)
        if count != 1:
            raise ValueError(
                f"{operation['path']}: source context {index} differs (matches={count}); "
                "review local changes before applying this version."
            )
        result = result.replace(old, new, 1)
    return result


def _prepare(source_root: Path | str) -> tuple[Path, str, list[dict[str, Any]]]:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir() or root.name != "src":
        raise ValueError("--source-root must point to an existing MaiBot src directory")
    version = _version(root)
    manifest = json.loads((HERE / "patches.json").read_text(encoding="utf-8"))
    if manifest["maibot_version"] != version:
        raise ValueError("Patch manifest and target version differ")
    changes = []
    for operation in manifest["files"]:
        target = _target(root, operation["path"])
        original = target.read_bytes() if target.exists() else None
        source = original.decode("utf-8-sig").replace("\r\n", "\n") if original is not None else ""
        if operation["kind"] == "add":
            payload = _target(HERE, operation["payload"])
            result = payload.read_text(encoding="utf-8")
            if original is not None and source != result:
                raise ValueError(f"{operation['path']}: an independently modified helper already exists")
        else:
            if original is None:
                raise ValueError(f"{operation['path']}: upstream source file is missing")
            result = _patched(source, operation)
        ast.parse(result, filename=operation["path"])
        newline = "\r\n" if original is not None and b"\r\n" in original else "\n"
        encoding = "utf-8-sig" if original is not None and original.startswith(b"\xef\xbb\xbf") else "utf-8"
        new_bytes = result.replace("\n", newline).encode(encoding)
        changes.append({"path": operation["path"], "target": target, "original": original,
                        "result": new_bytes, "changed": source != result})
    return root, version, changes


def plan(source_root: Path | str) -> dict[str, Any]:
    root, version, changes = _prepare(source_root)
    return {"mode": "preview", "source_root": str(root), "maibot_version": version,
            "changed_files": sum(item["changed"] for item in changes),
            "files": [{"path": item["path"], "changed": item["changed"]} for item in changes]}


def plan_install(source_root: Path | str) -> list[dict[str, Any]]:
    """Return in-memory edits for a caller that owns package-wide backup/apply."""
    _, _, changes = _prepare(source_root)
    return [{"target": item["target"], "relative_path": item["path"],
             "before": item["original"], "after": item["result"]}
            for item in changes if item["changed"]]


def _write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".daimon-memory-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def apply(source_root: Path | str, backup_root: Path | str | None = None) -> dict[str, Any]:
    root, version, prepared = _prepare(source_root)
    changes = [item for item in prepared if item["changed"]]
    if not changes:
        return {"mode": "apply", "maibot_version": version, "changed_files": 0, "backup": None}
    base = Path(backup_root).expanduser().resolve() if backup_root else root.parent / "daimon-memory-backups"
    backup = base / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup.mkdir(parents=True, exist_ok=False)
    entries = []
    for item in changes:
        exists = item["original"] is not None
        if exists:
            target = _target(backup, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["original"])
        entries.append({"path": item["path"], "existed": exists})
    receipt = {"source_root": str(root), "maibot_version": version, "files": entries}
    (backup / "backup.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written = []
    try:
        for item in changes:
            _write(item["target"], item["result"])
            written.append(item)
    except BaseException:
        for item in reversed(written):
            if item["original"] is None:
                item["target"].unlink(missing_ok=True)
            else:
                _write(item["target"], item["original"])
        raise
    return {"mode": "apply", "source_root": str(root), "maibot_version": version,
            "changed_files": len(changes), "backup": str(backup), "files": entries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()
    result = apply(args.source_root, args.backup_root) if args.apply else plan(args.source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
