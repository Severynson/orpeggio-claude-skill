"""Extract a single melody line and a bass line from a quantized score."""

from __future__ import annotations

import statistics
from collections import defaultdict

from arpeggio.schema import QNote, QTrack, Score

BASS_INSTRUMENTS = {"electric_bass", "acoustic_bass", "contrabass", "bass", "tuba"}
MELODY_PREFERENCE = ["voice", "melody", "synth_lead", "flutes", "violin", "trumpet",
                     "soprano_and_alto_sax", "tenor_sax", "clarinet", "oboe"]


def _abs(n: QNote, bpb: int) -> float:
    return (n.measure - 1) * bpb + n.beat


def _from_abs(b: float, bpb: int) -> tuple[int, float]:
    m = int(b // bpb) + 1
    return m, round(b - (m - 1) * bpb, 4)


def pick_melody_tracks(score: Score) -> list[str]:
    ids = {t.id: t for t in score.tracks if not t.is_drum and t.notes}
    for name in MELODY_PREFERENCE:
        for t in ids.values():
            if t.instrument == name and len(t.notes) >= 8:
                return [t.id]
    # otherwise: highest-median-pitch non-bass track with enough notes
    cands = [t for t in ids.values() if t.instrument not in BASS_INSTRUMENTS and len(t.notes) >= 8]
    if not cands:
        return []
    best = max(cands, key=lambda t: statistics.median(n.pitch for n in t.notes))
    return [best.id]


def pick_bass_tracks(score: Score) -> list[str]:
    ids = [t.id for t in score.tracks if t.instrument in BASS_INSTRUMENTS and t.notes]
    if ids:
        return ids
    return [t.id for t in score.tracks if not t.is_drum and t.notes]


def skyline(notes: list[QNote], bpb: int, highest: bool = True) -> list[QNote]:
    """Keep the top (or bottom) note of each onset and cut overlaps -> monophonic."""
    by_onset: dict[float, list[QNote]] = defaultdict(list)
    for n in notes:
        by_onset[round(_abs(n, bpb), 4)].append(n)
    onsets = sorted(by_onset)
    line: list[QNote] = []
    for t in onsets:
        group = by_onset[t]
        n = max(group, key=lambda x: x.pitch) if highest else min(group, key=lambda x: x.pitch)
        if line:
            prev = line[-1]
            p_on = _abs(prev, bpb)
            if p_on + prev.duration > t:
                # a lower note during a sustained higher one is accompaniment, not melody
                if highest and n.pitch < prev.pitch - 4 and p_on + prev.duration - t > 0.5:
                    continue
                if not highest and n.pitch > prev.pitch + 4 and p_on + prev.duration - t > 0.5:
                    continue
                prev.duration = round(max(0.125, t - p_on), 4)
        line.append(n.model_copy())
    return line


def extract_voices(score: Score, melody_tracks: list[str] | None = None,
                   bass_tracks: list[str] | None = None, melody_min_pitch: int = 52,
                   bass_max_pitch: int = 60) -> Score:
    bpb = score.beats_per_bar
    mel_ids = melody_tracks or pick_melody_tracks(score)
    bass_ids = bass_tracks or pick_bass_tracks(score)
    mel_src = [n for t in score.tracks if t.id in mel_ids for n in t.notes if n.pitch >= melody_min_pitch]
    bass_src = [n for t in score.tracks if t.id in bass_ids for n in t.notes if n.pitch <= bass_max_pitch]
    melody = skyline(mel_src, bpb, highest=True)
    bass = skyline(bass_src, bpb, highest=False)
    out = score.model_copy(deep=True)
    out.tracks = [QTrack(id="melody", instrument="melody", notes=melody,
                         **{"source_tracks": mel_ids}),
                  QTrack(id="bass", instrument="bass", notes=bass, **{"source_tracks": bass_ids})]
    return out
