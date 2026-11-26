"""Seconds -> measures/beats, using the detected beat grid (tempo changes OK)."""

from __future__ import annotations

import math

import numpy as np

from arpeggio.schema import Chords, Lyrics, QChord, QNote, QTrack, Rhythm, Score, Transcription
from arpeggio.theory import estimate_key, midi_to_name, prefers_flats
from arpeggio.transcription.rhythm import beat_position, beat_to_seconds


def quantize(tr: Transcription, rhythm: Rhythm, chords: Chords | None = None,
             lyrics: Lyrics | None = None, grid: int = 4, include_drums: bool = False,
             min_duration_steps: int = 1) -> Score:
    beats = np.asarray(rhythm.beats, float)
    tempo = rhythm.tempo_bpm
    bpb = rhythm.beats_per_bar
    step = 1.0 / grid

    # Anchor bar lines on the first reliable downbeat, extended backwards so the
    # earliest note falls inside measure 1 (a pickup becomes a mostly-empty bar).
    if rhythm.downbeats:
        d0 = float(np.round(beat_position(rhythm.downbeats[0], beats, tempo)))
    else:
        d0 = 0.0
    all_starts = [n.start for t in tr.tracks for n in t.notes]
    if chords:
        all_starts += [c.start for c in chords.chords if c.chord != "N"]
    earliest = min(all_starts) if all_starts else 0.0
    eb = float(beat_position(earliest, beats, tempo))
    m0 = d0 - math.ceil(max(0.0, d0 - eb) / bpb) * bpb

    def to_musical(t: float, snap: float) -> tuple[float, int, float]:
        b = float(beat_position(t, beats, tempo)) - m0
        b = round(b / snap) * snap
        measure = int(b // bpb) + 1
        return b, measure, round(b - (measure - 1) * bpb, 4)

    tracks: list[QTrack] = []
    pcs = np.zeros(12)
    last_beat = 0.0
    flats = False
    for t in tr.tracks:
        if t.is_drum and not include_drums:
            continue
        seen: dict[tuple[int, float], QNote] = {}
        for n in t.notes:
            b0, meas, inm = to_musical(n.start, step)
            b1 = round((float(beat_position(n.end, beats, tempo)) - m0) / step) * step
            dur = max(step * min_duration_steps, b1 - b0)
            if meas < 1:
                continue
            k = (n.pitch, b0)
            if k in seen and seen[k].duration >= dur:
                continue
            seen[k] = QNote(pitch=n.pitch, measure=meas, beat=inm, duration=round(dur, 4),
                            start=n.start, end=n.end, velocity=n.velocity)
            last_beat = max(last_beat, b0 + dur)
            if not t.is_drum:
                pcs[n.pitch % 12] += dur
        notes = sorted(seen.values(), key=lambda q: (q.measure, q.beat, q.pitch))
        tracks.append(QTrack(id=t.id, instrument=t.instrument, is_drum=t.is_drum, notes=notes))

    key_rank = estimate_key(list(pcs))
    key = key_rank[0]["key"] if key_rank else None
    flats = prefers_flats(key)
    for t in tracks:
        for n in t.notes:
            n.name = "drum" if t.is_drum else midi_to_name(n.pitch, flats)

    qchords: list[QChord] = []
    if chords:
        for c in chords.chords:
            if c.chord == "N":
                continue
            b0, meas, inm = to_musical(c.start, 0.5)
            b1 = round((float(beat_position(c.end, beats, tempo)) - m0) / 0.5) * 0.5
            if meas < 1 or b1 <= b0:
                continue
            if qchords and qchords[-1].chord == c.chord:
                qchords[-1].duration = round(b1 - ((qchords[-1].measure - 1) * bpb + qchords[-1].beat), 4)
                continue
            qchords.append(QChord(chord=c.chord, measure=meas, beat=inm, duration=round(b1 - b0, 4),
                                  start=c.start))
            last_beat = max(last_beat, b1)

    qlyrics = []
    if lyrics:
        for li, line in enumerate(lyrics.lines):
            for w in line.words:
                _, meas, inm = to_musical(w.start, step)
                qlyrics.append({"word": w.text, "line": li, "measure": meas, "beat": inm,
                                "start": w.start})

    measure_count = max(1, int(math.ceil(last_beat / bpb)))
    starts = beat_to_seconds(m0 + np.arange(measure_count) * bpb, beats, tempo)
    return Score(source=tr.source, tempo_bpm=round(tempo, 2), time_signature=f"{bpb}/4",
                 beats_per_bar=bpb, grid=f"1/{4 * grid}" if grid in (1, 2, 4, 8) else f"beat/{grid}",
                 measure_count=measure_count, measure_starts=[round(float(s), 3) for s in starts],
                 key=key, tracks=tracks, chords=qchords, lyrics=qlyrics)
