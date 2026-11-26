"""Beat, downbeat, tempo and meter detection (Beat This!, librosa fallback)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import file_sha1, torch_device
from arpeggio.schema import Provenance, Rhythm, Source


def detect_rhythm(audio: str | Path, backend: str = "auto", device: str | None = None,
                  beats_per_bar: int | None = None) -> Rhythm:
    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    import librosa

    sr = 22050
    y, _ = librosa.load(str(audio), sr=sr, mono=True)
    duration = len(y) / sr

    used = backend
    beats = downbeats = None
    if backend in ("auto", "beat_this"):
        try:
            from beat_this.inference import Audio2Beats

            dev = torch_device(device)
            if dev == "mps":
                dev = "cpu"  # beat_this is fast on CPU; MPS has had op gaps
            log(f"Beat This! on {dev}")
            a, b = Audio2Beats(checkpoint_path="final0", device=dev, dbn=False)(y, sr)
            a, b = np.asarray(a, float), np.asarray(b, float)
            beats, downbeats = (a, b) if len(a) >= len(b) else (b, a)
            used = "beat_this"
        except ImportError:
            if backend == "beat_this":
                raise ArpeggioError("beat_this not installed: run `uv sync --extra ml`")
        except Exception as e:  # pragma: no cover
            if backend == "beat_this":
                raise
            log(f"Beat This! failed ({e}); falling back to librosa")
    if beats is None:
        used = "librosa"
        _, bframes = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        beats = librosa.frames_to_time(bframes, sr=sr)
        downbeats = np.array([])

    if len(beats) < 4:
        raise ArpeggioError("too few beats detected; is the recording rhythmic and > 3 s long?")

    bpb = beats_per_bar or _estimate_beats_per_bar(beats, downbeats)
    if len(downbeats) == 0:
        downbeats = _downbeats_from_onsets(y, sr, beats, bpb)

    ibis = np.diff(beats)
    tempo = 60.0 / float(np.median(ibis))
    local = 60.0 / ibis
    steady = bool(np.std(local) / np.mean(local) < 0.06)
    curve = []
    # coarse tempo curve: one point per bar
    for i in range(0, len(ibis), max(1, bpb)):
        seg = ibis[i:i + bpb]
        curve.append({"time": round(float(beats[i]), 3), "bpm": round(60.0 / float(np.mean(seg)), 1)})
    return Rhythm(
        source=Source(path=str(audio.resolve()), sha1=file_sha1(audio),
                      duration_seconds=round(duration, 3)),
        provenance=Provenance(backend=used, model="final0" if used == "beat_this" else None),
        tempo_bpm=round(tempo, 2), beats_per_bar=bpb, time_signature=f"{bpb}/4",
        steady_tempo=steady,
        beats=[round(float(b), 4) for b in beats],
        downbeats=[round(float(d), 4) for d in downbeats],
        tempo_curve=curve)


def _estimate_beats_per_bar(beats: np.ndarray, downbeats: np.ndarray) -> int:
    if len(downbeats) < 2:
        return 4
    idx = np.searchsorted(beats, downbeats - 0.05)
    gaps = np.diff(idx)
    gaps = gaps[(gaps >= 2) & (gaps <= 7)]
    if len(gaps) == 0:
        return 4
    vals, counts = np.unique(gaps, return_counts=True)
    return int(vals[np.argmax(counts)])


def _downbeats_from_onsets(y, sr, beats, bpb) -> np.ndarray:
    """Pick the beat phase with the strongest low-frequency onsets."""
    import librosa

    env = librosa.onset.onset_strength(y=y, sr=sr, fmax=300)
    times = librosa.times_like(env, sr=sr)
    strength = np.interp(beats, times, env)
    best = max(range(bpb), key=lambda p: strength[p::bpb].mean())
    return beats[best::bpb]


def beat_position(t: float | np.ndarray, beats: np.ndarray, tempo_bpm: float):
    """Map seconds -> fractional beat index (linear extrapolation outside range)."""
    beats = np.asarray(beats, float)
    period = 60.0 / tempo_bpm
    t = np.asarray(t, float)
    idx = np.interp(t, beats, np.arange(len(beats), dtype=float))
    idx = np.where(t < beats[0], (t - beats[0]) / period, idx)
    idx = np.where(t > beats[-1], len(beats) - 1 + (t - beats[-1]) / period, idx)
    return idx


def beat_to_seconds(idx: float | np.ndarray, beats: np.ndarray, tempo_bpm: float):
    beats = np.asarray(beats, float)
    period = 60.0 / tempo_bpm
    idx = np.asarray(idx, float)
    t = np.interp(idx, np.arange(len(beats), dtype=float), beats)
    t = np.where(idx < 0, beats[0] + idx * period, t)
    t = np.where(idx > len(beats) - 1, beats[-1] + (idx - len(beats) + 1) * period, t)
    return t
