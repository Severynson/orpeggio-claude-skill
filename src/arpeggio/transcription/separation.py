"""Source separation with Demucs (optional extra: `uv sync --extra separation`).

Runs Demucs in-process and writes stems with soundfile (recent torchaudio
releases need torchcodec for saving, which the Demucs CLI relies on).
"""

from __future__ import annotations

from pathlib import Path

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import cache_dir, file_sha1, torch_device

ALL_STEMS = ("vocals", "drums", "bass", "other", "guitar", "piano")


def separate(audio: str | Path, stems: str = "all", model: str = "htdemucs",
             device: str | None = None, out_dir: str | Path | None = None) -> dict[str, str]:
    """Separate `audio` into stems. Returns {stem: wav_path}.

    stems="vocals" (or any single stem) also writes "no_<stem>" (the rest of the mix).
    model="htdemucs_6s" adds guitar and piano stems (chosen automatically for those).
    """
    try:
        import soundfile as sf
        import torch
        from demucs.apply import apply_model
        from demucs.audio import AudioFile
        from demucs.pretrained import get_model
    except ImportError as e:
        raise ArpeggioError("Demucs not installed: run `uv sync --extra separation`") from e
    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    if stems in ("guitar", "piano") and model == "htdemucs":
        model = "htdemucs_6s"
    sha = file_sha1(audio)[:16]
    target = (Path(out_dir) if out_dir else cache_dir() / "stems" / sha) / model
    target.mkdir(parents=True, exist_ok=True)
    wanted = None if stems == "all" else stems

    def existing() -> dict[str, str]:
        return {p.stem: str(p) for p in sorted(target.glob("*.wav"))}

    have = existing()
    if (wanted and wanted in have) or (not wanted and len(have) >= 4):
        log(f"using cached stems in {target}")
        return _select(have, wanted)

    log(f"Demucs {model}: loading model")
    m = get_model(model)
    m.eval()
    if wanted and wanted not in m.sources:
        raise ArpeggioError(f"model {model} has no '{wanted}' stem (has {', '.join(m.sources)})")
    wav = AudioFile(str(audio)).read(streams=0, samplerate=m.samplerate, channels=m.audio_channels)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    x = (wav - mean) / std
    dev = torch_device(device)
    log(f"separating on {dev} (a 3-minute song takes ~1–2 min on CPU)")
    try:
        with torch.no_grad():
            out = apply_model(m, x[None], device=dev, shifts=1, split=True, overlap=0.25, progress=False)[0]
    except Exception as e:
        if dev == "cpu":
            raise
        log(f"Demucs on {dev} failed ({e}); retrying on CPU")
        with torch.no_grad():
            out = apply_model(m, x[None], device="cpu", shifts=1, split=True, overlap=0.25, progress=False)[0]
    out = out * std + mean
    for name, src in zip(m.sources, out):
        sf.write(target / f"{name}.wav", src.cpu().numpy().T, m.samplerate)
    for name in m.sources:  # complementary "no_<stem>" mixes
        rest = sum(src for n, src in zip(m.sources, out) if n != name)
        if name == wanted or (not wanted and name == "vocals"):
            sf.write(target / f"no_{name}.wav", rest.cpu().numpy().T, m.samplerate)
    return _select(existing(), wanted)


def _select(have: dict[str, str], wanted: str | None) -> dict[str, str]:
    if not wanted:
        return have
    return {k: v for k, v in have.items() if k in (wanted, f"no_{wanted}")}
