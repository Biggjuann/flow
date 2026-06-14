"""Prompt loader — all prompts live as markdown files in prompts/.

Templates use ``string.Template`` placeholders (``$name`` / ``${name}``) so
markdown and JSON braces in prompt bodies never need escaping.
"""
from __future__ import annotations

import os
from pathlib import Path
from string import Template


def prompts_dir() -> Path:
    env = os.environ.get("ADENGINE_PROMPTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(prompt_name: str, /, **variables: str) -> str:
    path = prompts_dir() / f"{prompt_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    text = path.read_text()
    if variables:
        text = Template(text).safe_substitute(**variables)
    return text
