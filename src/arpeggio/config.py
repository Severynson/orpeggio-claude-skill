"""Runtime configuration: environment variables, cache and work directories."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from dotenv import load_dotenv

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load `.env` from the current directory, then from the repository root.

    Existing environment variables always win, so a key exported in the shell
    (or configured in a cloud session) is never overridden by a file.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(_PACKAGE_ROOT / ".env", override=False)


def cache_dir() -> Path:
    """Persistent cache for expensive model outputs (keyed by audio hash)."""
    root = Path(os.environ.get("ARPEGGIO_CACHE", Path.home() / ".cache" / "arpeggio"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def work_dir_for(audio_or_name: str | Path) -> Path:
    """Default per-song working directory: ./arpeggio-work/<slug>/."""
    base = Path(os.environ.get("ARPEGGIO_WORKDIR", Path.cwd() / "arpeggio-work"))
    d = base / slugify(Path(audio_or_name).stem)
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or "song"


def file_sha1(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def torch_device(preferred: str | None = None) -> str:
    """Pick a torch device string: explicit > CUDA > MPS > CPU."""
    if preferred and preferred != "auto":
        return preferred
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover - torch missing
        pass
    return "cpu"
