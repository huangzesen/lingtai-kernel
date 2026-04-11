"""Psyche capability — identity, memory, and context management.

Upgrades the eigen intrinsic with:
- Evolving identity (covenant + character)
- Enhanced memory.edit with file import (files param)
- memory.append — pin files as read-only reference (persisted in system/memory_append.json)
- memory.load delegation to eigen (auto-appends pinned files)
- Context molt delegation to eigen

Library is a separate standalone capability.

Usage:
    agent = Agent(capabilities=["psyche"])
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}

def get_description(lang: str = "en") -> str:
    return t(lang, "psyche.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "object": {
                "type": "string",
                "enum": ["lingtai", "memory", "context"],
                "description": t(lang, "psyche.object"),
            },
            "action": {
                "type": "string",
                "enum": ["update", "load", "edit", "append", "molt"],
                "description": t(lang, "psyche.action"),
            },
            "content": {
                "type": "string",
                "description": t(lang, "psyche.content"),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "psyche.files"),
            },
            "summary": {
                "type": "string",
                "description": t(lang, "psyche.summary"),
            },
        },
        "required": ["object", "action"],
    }



class PsycheManager:
    """Identity, memory, and context manager."""

    def __init__(self, agent: "BaseAgent", eigen_handler):
        self._agent = agent
        self._working_dir = agent._working_dir
        self._eigen_handler = eigen_handler

        # Paths
        system_dir = self._working_dir / "system"
        self._covenant_path = system_dir / "covenant.md"
        self._character_path = system_dir / "lingtai.md"

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    _VALID_ACTIONS: dict[str, set[str]] = {
        "lingtai": {"update", "load"},
        "memory": {"edit", "load", "append"},
        "context": {"molt"},
    }

    def handle(self, args: dict) -> dict:
        obj = args.get("object", "")
        action = args.get("action", "")

        valid = self._VALID_ACTIONS.get(obj)
        if valid is None:
            return {
                "error": f"Unknown object: {obj!r}. "
                f"Must be one of: {', '.join(sorted(self._VALID_ACTIONS))}.",
            }
        if action not in valid:
            return {
                "error": f"Invalid action {action!r} for {obj}. "
                f"Valid actions: {', '.join(sorted(valid))}.",
            }

        method = getattr(self, f"_{obj}_{action}")
        return method(args)

    # ------------------------------------------------------------------
    # Lingtai (identity/character) actions
    # ------------------------------------------------------------------

    def _lingtai_update(self, args: dict) -> dict:
        content = args.get("content", "")
        self._character_path.parent.mkdir(exist_ok=True)
        self._character_path.write_text(content)
        # Auto-load into system prompt
        self._lingtai_load({})
        return {"status": "ok", "path": str(self._character_path)}

    def _lingtai_load(self, _args: dict) -> dict:
        covenant = ""
        if self._covenant_path.is_file():
            covenant = self._covenant_path.read_text()
        character = self._character_path.read_text() if self._character_path.is_file() else ""

        parts = [p for p in [covenant, character] if p.strip()]
        combined = "\n\n".join(parts)

        if combined.strip():
            self._agent._prompt_manager.write_section(
                "covenant", combined, protected=True,
            )
        else:
            self._agent._prompt_manager.delete_section("covenant")
        self._agent._token_decomp_dirty = True
        self._agent._flush_system_prompt()

        rel_path = "system/lingtai.md"
        git_diff = self._agent._workdir.diff(rel_path)

        self._agent._log(
            "psyche_character_load",
            changed=bool(git_diff),
        )

        return {
            "status": "ok",
            "size_bytes": len(combined.encode("utf-8")),
            "content_preview": combined[:200],
            "diff": {
                "changed": bool(git_diff),
                "git_diff": git_diff or "",
                "commit": None,
            },
        }

    # ------------------------------------------------------------------
    # Memory actions — upgrade eigen with files support
    # ------------------------------------------------------------------

    def _memory_edit(self, args: dict) -> dict:
        """Write content + optional file imports to memory.md."""
        content = args.get("content", "")
        files = args.get("files") or []

        parts = [content] if content else []

        # Append file contents with numbered dividers
        not_found = []
        for i, fpath in enumerate(files, start=1):
            if os.path.isabs(fpath):
                resolved = Path(fpath)
            else:
                resolved = self._working_dir / fpath
            if not resolved.is_file():
                not_found.append(fpath)
                continue
            file_content = resolved.read_text()
            parts.append(f"[file-{i}]\n{file_content}")

        if not_found:
            return {"error": f"Files not found: {', '.join(not_found)}"}

        if not parts:
            return {"error": "Provide content, files, or both."}

        combined = "\n\n".join(parts)

        # Delegate to eigen for the actual write (eigen auto-loads into prompt)
        return self._eigen_handler({
            "object": "memory", "action": "edit", "content": combined,
        })

    # ------------------------------------------------------------------
    # Memory append — pin files as read-only reference
    # ------------------------------------------------------------------

    _APPEND_LIST_PATH = "system/memory_append.json"
    _APPEND_TOKEN_LIMIT = 100_000

    @property
    def _append_list_file(self) -> Path:
        return self._working_dir / self._APPEND_LIST_PATH

    def _load_append_list(self) -> list[str]:
        """Read the persisted append file list (empty list if missing)."""
        if not self._append_list_file.is_file():
            return []
        try:
            data = json.loads(self._append_list_file.read_text())
            if isinstance(data, list):
                return [str(p) for p in data]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save_append_list(self, files: list[str]) -> None:
        """Persist the append file list to disk."""
        self._append_list_file.parent.mkdir(exist_ok=True)
        self._append_list_file.write_text(json.dumps(files, ensure_ascii=False))

    def _resolve_path(self, fpath: str) -> Path:
        if os.path.isabs(fpath):
            return Path(fpath)
        return self._working_dir / fpath

    def _read_append_content(self, files: list[str]) -> tuple[str, list[str]]:
        """Read all append files. Returns (combined content, not_found list)."""
        parts: list[str] = []
        not_found: list[str] = []
        for fpath in files:
            resolved = self._resolve_path(fpath)
            if not resolved.is_file():
                not_found.append(fpath)
                continue
            parts.append(f"[append: {fpath}]\n{resolved.read_text()}")
        return "\n\n".join(parts), not_found

    @staticmethod
    def _is_text_file(path: Path, sample_size: int = 8192) -> bool:
        """Check if a file is a text file by reading the first chunk."""
        try:
            chunk = path.read_bytes()[:sample_size]
        except OSError:
            return False
        # Null bytes are a strong binary indicator
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def _memory_append(self, args: dict) -> dict:
        """Set the list of files pinned as read-only memory reference.

        Pass files=[] to clear. Persisted to system/memory_append.json.
        Automatically reloads memory after updating the list.
        Only text files are accepted.
        """
        files = args.get("files")
        if files is None:
            # No files param — return current list
            current = self._load_append_list()
            return {"status": "ok", "files": current, "count": len(current)}

        # Validate all files exist and are text
        not_found: list[str] = []
        not_text: list[str] = []
        for fpath in files:
            resolved = self._resolve_path(fpath)
            if not resolved.is_file():
                not_found.append(fpath)
            elif not self._is_text_file(resolved):
                not_text.append(fpath)
        if not_found:
            return {"error": f"Files not found: {', '.join(not_found)}"}
        if not_text:
            return {"error": f"Only text files are accepted. Binary files: {', '.join(not_text)}"}

        if files:
            # Token guard
            from lingtai_kernel.token_counter import count_tokens
            combined, _ = self._read_append_content(files)
            tokens = count_tokens(combined)
            if tokens > self._APPEND_TOKEN_LIMIT:
                return {
                    "error": f"Append files total {tokens:,} tokens, "
                    f"exceeding the {self._APPEND_TOKEN_LIMIT:,} token limit. "
                    f"Reduce the number or size of files.",
                }

        # Persist and reload
        self._save_append_list(files)
        self._memory_load({})

        action = "cleared" if not files else "set"
        return {"status": "ok", "action": action, "files": files, "count": len(files)}

    def _memory_load(self, args: dict) -> dict:
        """Load memory.md + appended reference files into prompt."""
        # First, delegate to eigen for the base memory.md load
        result = self._eigen_handler({"object": "memory", "action": "load"})

        # Then append pinned files (read-only) to the memory section
        append_files = self._load_append_list()
        if append_files:
            append_content, not_found = self._read_append_content(append_files)
            if append_content:
                existing = self._agent._prompt_manager.read_section("memory") or ""
                combined = existing + "\n\n---\n# 📎 Reference (read-only)\n\n" + append_content
                self._agent._prompt_manager.write_section("memory", combined)
                self._agent._token_decomp_dirty = True
                self._agent._flush_system_prompt()
            if not_found:
                result["append_not_found"] = not_found

            result["append_files"] = append_files
            result["append_count"] = len(append_files)

        return result

    # ------------------------------------------------------------------
    # Context actions — delegate to eigen
    # ------------------------------------------------------------------

    def _context_molt(self, args: dict) -> dict:
        return self._eigen_handler({"object": "context", "action": "molt", "summary": args.get("summary")})


def setup(agent: "BaseAgent") -> PsycheManager:
    """Set up psyche capability — identity, memory, and context management."""
    lang = agent._config.language
    eigen_handler = agent.override_intrinsic("eigen")
    agent._eigen_owns_memory = True

    mgr = PsycheManager(agent, eigen_handler)

    # Auto-load character and memory into system prompt at boot
    mgr._lingtai_load({})
    mgr._memory_load({})

    # Register post-molt hook to reload character + memory
    if not hasattr(agent, "_post_molt_hooks"):
        agent._post_molt_hooks = []
    agent._post_molt_hooks.append(lambda: (mgr._lingtai_load({}), mgr._memory_load({})))

    agent.add_tool(
        "psyche", schema=get_schema(lang), handler=mgr.handle, description=get_description(lang),
    )
    return mgr
