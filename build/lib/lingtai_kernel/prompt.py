"""System prompt — section manager + builder.

SystemPromptManager manages named sections of an agent's system prompt.
Sections are rendered in a configurable order. The default order is:
    principle (no header) → covenant → rules → tools → procedures → brief → skills → identity → memory → comment

build_system_prompt() assembles base_prompt + rendered sections.
"""
from __future__ import annotations

from typing import Optional


class SystemPromptManager:
    """Manages named sections of an agent's system prompt.

    Sections can be marked as protected (host-written, not overwritable by the LLM)
    or unprotected (LLM-writable at runtime).

    Render order is configurable via set_order(). Sections not in the order
    list are rendered between the ordered sections and the tail. The last
    name in the order list is always rendered last (typically 'memory').
    """

    # Default render order. First entry rendered without ## header (raw text).
    _DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "procedures", "brief", "skills", "identity", "memory", "comment"]

    def __init__(self) -> None:
        self._sections: dict[str, dict] = {}
        self._order: list[str] = list(self._DEFAULT_ORDER)
        # First entry in order is rendered without ## header (raw text)
        self._raw_sections: set[str] = {"principle"}

    def write_section(self, name: str, content: str, protected: bool = False) -> None:
        """Write a section (host API — bypasses protection checks)."""
        self._sections[name] = {"content": content, "protected": protected}

    def read_section(self, name: str) -> Optional[str]:
        """Read a section's content, or None if not found."""
        entry = self._sections.get(name)
        return entry["content"] if entry else None

    def delete_section(self, name: str) -> bool:
        """Delete a section. Returns True if it existed."""
        return self._sections.pop(name, None) is not None

    def list_sections(self) -> list[dict]:
        """Return a list of section metadata dicts."""
        return [
            {"name": name, "protected": entry["protected"], "length": len(entry["content"])}
            for name, entry in self._sections.items()
        ]

    def set_order(self, names: list[str]) -> None:
        """Set the render order. Last name is always rendered last."""
        self._order = list(names)

    def set_raw(self, name: str) -> None:
        """Mark a section as raw — rendered without ## header."""
        self._raw_sections.add(name)

    def render(self) -> str:
        """Render all sections into a single string following the configured order.

        - Sections in self._order are rendered in that order.
        - The last name in self._order is always rendered last (tail).
        - Sections not in self._order are rendered between ordered and tail.
        - Sections in self._raw_sections are rendered without ## header.
        - Empty/missing sections are skipped.
        """
        ordered: list[str] = []
        tail_name = self._order[-1] if self._order else None
        head_names = self._order[:-1] if self._order else []

        # Render head sections in order
        for name in head_names:
            entry = self._sections.get(name)
            if not entry:
                continue
            if name in self._raw_sections:
                ordered.append(entry["content"])
            else:
                ordered.append(f"## {name}\n{entry['content']}")

        # Render unordered sections (not in order list)
        all_ordered = set(self._order)
        for name, entry in self._sections.items():
            if name in all_ordered:
                continue
            if name in self._raw_sections:
                ordered.append(entry["content"])
            else:
                ordered.append(f"## {name}\n{entry['content']}")

        # Render tail section last
        if tail_name:
            entry = self._sections.get(tail_name)
            if entry:
                if tail_name in self._raw_sections:
                    ordered.append(entry["content"])
                else:
                    ordered.append(f"## {tail_name}\n{entry['content']}")

        return "\n\n".join(ordered)


def build_system_prompt(
    prompt_manager: SystemPromptManager,
    base_prompt: str = "",
    language: str = "en",
) -> str:
    """Build the full system prompt from components.

    Order: base prompt → sections.
    base_prompt is framework-level guidance injected by the wrapper package (lingtai).
    """
    parts = []
    if base_prompt:
        parts.append(base_prompt)

    sections_text = prompt_manager.render()
    if sections_text:
        parts.append(sections_text)

    return "\n\n---\n\n".join(parts)
