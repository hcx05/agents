from __future__ import annotations

from typing import Dict, Type

from ..config import AgentCfg
from .agy import AgyAdapter
from .base import Adapter
from .claude import ClaudeAdapter
from .fake import FakeAdapter

REGISTRY: Dict[str, Type[Adapter]] = {
    "claude": ClaudeAdapter,
    "agy": AgyAdapter,
    "fake": FakeAdapter,
}


def get_adapter(cfg: AgentCfg) -> Adapter:
    try:
        return REGISTRY[cfg.cli](cfg)
    except KeyError:
        raise ValueError(f"unsupported cli {cfg.cli!r}")
