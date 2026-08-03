"""Regenerate the checked-in Markdown mirror of arctl's built-in help."""

from pathlib import Path

from arctl.cli import render_cli_reference


ROOT = Path(__file__).resolve().parents[1]
(ROOT / "docs" / "cli-reference.md").write_text(
    render_cli_reference(), encoding="utf-8"
)
