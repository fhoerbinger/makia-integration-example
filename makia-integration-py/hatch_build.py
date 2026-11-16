"""Hatch build hook: ship MAKIA's API description with the guide.

Copies `apps/audit-api/openapi.json` from the monorepo into the package docs when it is
there (a build from a source archive without the monorepo skips it)."""

from __future__ import annotations

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SOURCE = Path("..") / ".." / "apps" / "audit-api" / "openapi.json"
TARGET = Path("makia_integration") / "docs" / "openapi.json"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        source = (root / SOURCE).resolve()
        if source.is_file():
            shutil.copyfile(source, root / TARGET)
            build_data.setdefault("artifacts", []).append(TARGET.as_posix())
