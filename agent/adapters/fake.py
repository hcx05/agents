"""Fake CLI adapter: speaks Claude's headless contract but runs agent/adapters/fake_cli.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

from .. import runner
from .base import Invocation, TurnRequest
from .claude import ClaudeAdapter


class FakeAdapter(ClaudeAdapter):
    name = "fake"
    display_name = "Fake"

    def version_args_supported(self) -> bool:
        return False

    def command_prefix(self) -> List[str]:
        cmd = self.cfg.command or os.environ.get("AGENT_FAKE_CLI")
        if cmd:
            return runner.resolve_command(cmd)
        return [sys.executable, str(Path(__file__).with_name("fake_cli.py"))]

    def build(self, req: TurnRequest, prefix: List[str]) -> Invocation:
        inv = super().build(req, prefix)
        inv.args += ["--fake-agent", req.agent_id]
        return inv
