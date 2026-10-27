"""Load the three TOML config files and apply command-line overrides.

Everything the rest of the code needs about "which model, which endpoint,
which tools, what to test" comes from here, so it is the first file to read.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # project root
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"


@dataclass
class ModelSpec:
    """One LLM endpoint from models.toml."""
    name: str
    base_url: str
    model: str
    api_key_env: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_body: dict = field(default_factory=dict)


@dataclass
class RoleSpec:
    """One agent role from roles.toml, with its model already resolved."""
    name: str
    model: ModelSpec
    prompt: str          # the system prompt text (file contents, not the path)
    tools: list[str]
    max_turns: int


@dataclass
class Scenario:
    """What one perf-test run is trying to achieve. The planner gets this as its goal."""
    name: str = "default"
    slo_ttft_ms: float = 500
    budget: int = 8                      # max configs to deploy, failed deploys included
    objective: str = "throughput"        # "throughput" or "throughput_per_gpu"


def _read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_models() -> dict[str, ModelSpec]:
    raw = _read_toml(CONFIG_DIR / "models.toml")["models"]
    return {name: ModelSpec(name=name, **fields) for name, fields in raw.items()}


def load_target() -> dict:
    return _read_toml(CONFIG_DIR / "target.toml")


def parse_overrides(pairs: list[str] | None) -> dict[str, str]:
    """Turn ["planner=muse-spark", "analyst=beam"] into {"planner": "muse-spark", ...}."""
    out = {}
    for pair in pairs or []:
        role, sep, model = pair.partition("=")
        if not sep or not role or not model:
            raise SystemExit(f"Bad --model value {pair!r}; expected role=model, e.g. planner=beam")
        out[role.strip()] = model.strip()
    return out


def load_roles(overrides: dict[str, str] | None = None) -> dict[str, RoleSpec]:
    """Build every role, applying `overrides` ({role: model_name}) on top of roles.toml."""
    models = load_models()
    raw = _read_toml(CONFIG_DIR / "roles.toml")["roles"]
    overrides = overrides or {}

    unknown_roles = set(overrides) - set(raw)
    if unknown_roles:
        raise SystemExit(f"Unknown role(s) in override: {sorted(unknown_roles)}. Roles: {sorted(raw)}")

    roles = {}
    for name, spec in raw.items():
        model_name = overrides.get(name, spec["model"])
        if model_name not in models:
            raise SystemExit(f"Role {name!r} uses model {model_name!r}, which is not in models.toml. "
                             f"Known models: {sorted(models)}")
        roles[name] = RoleSpec(
            name=name,
            model=models[model_name],
            prompt=(PROMPTS_DIR / spec["prompt"]).read_text(),
            tools=list(spec.get("tools", [])),
            max_turns=int(spec.get("max_turns", 10)),
        )
    return roles
