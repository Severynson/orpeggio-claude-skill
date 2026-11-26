"""Reading/writing JSON artifacts and printing compact command results."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from arpeggio.schema import KINDS


class ArpeggioError(RuntimeError):
    """An expected, user-facing failure (missing dependency, bad input...)."""


def load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ArpeggioError(f"file not found: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load(path: str | Path, expect: str | tuple[str, ...] | None = None) -> BaseModel:
    data = load_json(path)
    kind = data.get("kind")
    if expect:
        allowed = (expect,) if isinstance(expect, str) else expect
        if kind not in allowed:
            raise ArpeggioError(f"{path}: expected kind {allowed}, got '{kind}'")
    model = KINDS.get(kind)
    if model is None:
        raise ArpeggioError(f"{path}: unknown artifact kind '{kind}'")
    return model.model_validate(data)


def save(obj: BaseModel | dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = obj.model_dump(mode="json", exclude_none=True) if isinstance(obj, BaseModel) else obj
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return p


def emit(result: dict[str, Any]) -> None:
    """Print a compact JSON result on stdout (the agent reads this)."""
    sys.stdout.write(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=1) + "\n")


def emit_error(message: str, hint: str | None = None) -> None:
    payload = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def log(msg: str) -> None:
    """Progress messages go to stderr so stdout stays machine-readable."""
    print(f"[arpeggio] {msg}", file=sys.stderr, flush=True)
