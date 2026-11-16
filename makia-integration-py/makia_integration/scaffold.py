"""`makia-integration new <dir>`: write the starter integration from the shipped template."""

from __future__ import annotations

from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent / "docs" / "template"
SUFFIX = ".tmpl"  # keeps pytest, ruff and the wheel from treating the template as code
# Dotfiles are skipped by some build tools, so they ship under a plain name.
OUTPUT_NAMES = {"env.example": ".env.example"}


def new_integration(target: Path) -> list[Path]:
    """Write the template files into `target` (created if missing).

    Raises FileExistsError when `target` exists and holds anything, and
    NotADirectoryError when it is a file. Returns the files written."""
    target = Path(target)
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"{target} is a file, not a folder")
    if target.is_dir() and any(target.iterdir()):
        raise FileExistsError(f"{target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for source in sorted(TEMPLATE_DIR.glob(f"*{SUFFIX}")):
        text = source.read_text(encoding="utf-8")
        name = source.name.removesuffix(SUFFIX)
        out = target / OUTPUT_NAMES.get(name, name)
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        written.append(out)
    return written
