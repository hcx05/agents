"""Config loading/validation. File: %APPDATA%\\agent\\config.json."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SUPPORTED_CLIS = ("claude", "agy", "fake")

DEFAULT_CONFIG: Dict[str, Any] = {
    "manager": "manager",
    "agents": {
        "manager": {"role": "manager", "cli": "claude", "model": None, "effort": None},
        "w1": {"role": "worker", "cli": "agy", "model": None, "effort": None},
    },
    "turn_timeout_sec": 1800,
    "max_manager_turns_per_input": 30,
}

AGENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
RESERVED_IDS = {"me", "user", "system"}


class ConfigError(Exception):
    pass


@dataclass
class AgentCfg:
    id: str
    role: str
    cli: str
    model: Optional[str] = None
    effort: Optional[str] = None
    command: Optional[str] = None  # explicit executable path override
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    manager: str
    agents: Dict[str, AgentCfg]
    turn_timeout_sec: float = 1800
    max_manager_turns_per_input: int = 30
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def workers(self) -> List[AgentCfg]:
        return [a for a in self.agents.values() if a.role == "worker"]


def home_dir() -> Path:
    """Directory holding config.json and sessions/. AGENT_HOME overrides (tests)."""
    override = os.environ.get("AGENT_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "agent"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "agent"


def config_path() -> Path:
    return home_dir() / "config.json"


def ensure_config(path: Optional[Path] = None) -> Path:
    path = path or config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _opt_str(agent_id: str, key: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"agents.{agent_id}.{key} must be null or a non-empty string")
    return value


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, dict) or not agents_raw:
        raise ConfigError("'agents' must be a non-empty object")
    agents: Dict[str, AgentCfg] = {}
    for aid, a in agents_raw.items():
        if not AGENT_ID_RE.match(aid) or aid.lower() in RESERVED_IDS:
            raise ConfigError(f"invalid agent id {aid!r} (letters/digits/_/-, must start with a letter, not reserved)")
        if not isinstance(a, dict):
            raise ConfigError(f"agents.{aid} must be an object")
        role = a.get("role")
        if role not in ("manager", "worker"):
            raise ConfigError(f"agents.{aid}.role must be 'manager' or 'worker'")
        cli = a.get("cli")
        if cli not in SUPPORTED_CLIS:
            raise ConfigError(f"agents.{aid}.cli {cli!r} unsupported; supported: {', '.join(SUPPORTED_CLIS)}")
        options = a.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError(f"agents.{aid}.options must be an object")
        agents[aid] = AgentCfg(
            id=aid,
            role=role,
            cli=cli,
            model=_opt_str(aid, "model", a.get("model")),
            effort=_opt_str(aid, "effort", a.get("effort")),
            command=_opt_str(aid, "command", a.get("command")),
            options=options,
        )
    manager = raw.get("manager")
    if manager not in agents or agents[manager].role != "manager":
        raise ConfigError("'manager' must name an agent whose role is 'manager'")
    if sum(1 for a in agents.values() if a.role == "manager") != 1:
        raise ConfigError("exactly one agent with role 'manager' is required")
    workers = [a for a in agents.values() if a.role == "worker"]
    if len(workers) != 1:
        raise ConfigError("MVP v1 supports exactly one worker")
    timeout = raw.get("turn_timeout_sec", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("turn_timeout_sec must be a positive number")
    turns = raw.get("max_manager_turns_per_input", 30)
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
        raise ConfigError("max_manager_turns_per_input must be a positive integer")
    snap = raw.get("snapshot", {})
    if not isinstance(snap, dict):
        raise ConfigError("snapshot must be an object")
    return Config(manager=manager, agents=agents, turn_timeout_sec=timeout,
                  max_manager_turns_per_input=turns, snapshot=snap)


def load_config(path: Optional[Path] = None, create: bool = True) -> Config:
    path = path or config_path()
    if create:
        ensure_config(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"config is not valid JSON ({path}): {e}")
    try:
        return parse_config(raw)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}")
