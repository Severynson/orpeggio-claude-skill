"""Chord recognition: LV-Chordia (neural, large vocabulary) or chroma templates."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import numpy as np

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import file_sha1
from arpeggio.schema import ChordSegment, Chords, Provenance, Source
from arpeggio.theory import CHORD_QUALITIES, NOTE_NAMES_SHARP, harte_to_symbol


def detect_chords(audio: str | Path, backend: str = "auto", vocabulary: str = "submission",
                  start: float | None = None, end: float | None = None,
                  min_duration: float = 0.3) -> Chords:
    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    segs: list[ChordSegment] | None = None
    used = backend
    if backend in ("auto", "lv-chordia"):
        try:
            from lv_chordia import chord_recognition

            log(f"LV-Chordia ({vocabulary} vocabulary)")
            with contextlib.redirect_stdout(io.StringIO()):
                raw = chord_recognition(str(audio), vocabulary)
            segs = [ChordSegment(chord=harte_to_symbol(r["chord"]), start=r["start_time"],
                                 end=r["end_time"], raw_label=r["chord"]) for r in raw]
            used = "lv-chordia"
        except ImportError:
            if backend == "lv-chordia":
                raise ArpeggioError("lv-chordia not installed: run `uv sync --extra ml`")
        except Exception as e:  # pragma: no cover
            if backend == "lv-chordia":
                raise
            log(f"LV-Chordia failed ({e}); falling back to chroma templates")
    if segs is None:
        segs = _chroma_chords(audio)
        used = "chroma"

    segs = _merge(segs, min_duration)
    if start is not None or end is not None:
        lo, hi = start or 0.0, end if end is not None else 1e9
        segs = [s for s in segs if s.end > lo and s.start < hi]
    return Chords(source=Source(path=str(audio.resolve()), sha1=file_sha1(audio)),
                  provenance=Provenance(backend=used,
                                        params={"vocabulary": vocabulary} if used == "lv-chordia" else {}),
                  chords=segs)


def _merge(segs: list[ChordSegment], min_duration: float) -> list[ChordSegment]:
    """Merge identical neighbours and absorb very short blips."""
    out: list[ChordSegment] = []
    for s in segs:
        if out and out[-1].chord == s.chord:
            out[-1].end = s.end
        else:
            out.append(s.model_copy())
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, s in enumerate(out):
            if s.end - s.start < min_duration and s.chord != "N":
                if i > 0:
                    out[i - 1].end = s.end
                else:
                    out[i + 1].start = s.start
                out.pop(i)
                changed = True
                break
        merged: list[ChordSegment] = []
        for s in out:
            if merged and merged[-1].chord == s.chord:
                merged[-1].end = s.end
            else:
                merged.append(s)
        out = merged
    for s in out:
        s.start, s.end = round(s.start, 3), round(s.end, 3)
    return out


_TEMPLATE_QUALITIES = ["", "m", "7", "m7", "maj7", "sus4", "dim"]


def _templates():
    names, mats = [], []
    for q in _TEMPLATE_QUALITIES:
        for root in range(12):
            v = np.zeros(12)
            for k, i in enumerate(CHORD_QUALITIES[q]):
                v[(root + i) % 12] = 1.0 if k < 3 else 0.7
            # triads slightly preferred over sevenths
            prior = 1.0 if q in ("", "m") else 0.93
            names.append(NOTE_NAMES_SHARP[root] + q)
            mats.append(prior * v / np.linalg.norm(v))
    return names, np.array(mats)


def _chroma_chords(audio: Path) -> list[ChordSegment]:
    import librosa

    log("chroma-template chord recognition (fallback)")
    sr, hop = 22050, 2048
    y, _ = librosa.load(str(audio), sr=sr, mono=True)
    yh = librosa.effects.harmonic(y)
    chroma = librosa.feature.chroma_cqt(y=yh, sr=sr, hop_length=hop)
    chroma = librosa.decompose.nn_filter(chroma, aggregate=np.median, metric="cosine")
    names, T = _templates()
    norm = chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9)
    scores = T @ norm  # (n_chords, frames)
    energy = np.linalg.norm(chroma, axis=0)
    # Viterbi with a sticky transition matrix
    n = len(names) + 1  # + "N"
    emis = np.vstack([scores, np.full((1, scores.shape[1]), 0.35)])
    emis[-1, energy < 0.1 * energy.max()] = 1.0
    logp = np.log(np.maximum(emis, 1e-6) ** 8)
    stay = np.log(0.9)
    move = np.log(0.1 / (n - 1))
    dp = logp[:, 0].copy()
    back = np.zeros((n, logp.shape[1]), dtype=int)
    for t in range(1, logp.shape[1]):
        best_prev = int(np.argmax(dp))
        cand_move = dp[best_prev] + move
        stay_scores = dp + stay
        choose_stay = stay_scores >= cand_move
        back[:, t] = np.where(choose_stay, np.arange(n), best_prev)
        dp = np.where(choose_stay, stay_scores, cand_move) + logp[:, t]
    path = [int(np.argmax(dp))]
    for t in range(logp.shape[1] - 1, 0, -1):
        path.append(back[path[-1], t])
    path.reverse()
    times = librosa.frames_to_time(np.arange(len(path) + 1), sr=sr, hop_length=hop)
    segs = []
    for i, p in enumerate(path):
        label = names[p] if p < len(names) else "N"
        if segs and segs[-1].chord == label:
            segs[-1].end = float(times[i + 1])
        else:
            segs.append(ChordSegment(chord=label, start=float(times[i]), end=float(times[i + 1])))
    return segs
