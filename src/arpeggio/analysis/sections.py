"""Hierarchical access to artifacts: compact overviews and section slices.

Level 1 `summarize`  — key, tempo, tracks, chord progression, structure hints.
Level 2 `section`    — a measure / time range in compact text.
Level 3 the JSON file itself.
"""

from __future__ import annotations

from collections import Counter

from arpeggio.schema import Arrangement, Chords, Lyrics, Rhythm, Score, Transcription
from arpeggio.theory import midi_to_name, name_to_midi, prefers_flats


def _fmt(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return (s[1:] if s.startswith("0.") else s) or "0"


def summarize(obj) -> dict:
    if isinstance(obj, Transcription):
        from arpeggio.transcription.notes import summarize_transcription

        return summarize_transcription(obj)
    if isinstance(obj, Rhythm):
        return {"tempo_bpm": obj.tempo_bpm, "time_signature": obj.time_signature,
                "steady_tempo": obj.steady_tempo, "beats": len(obj.beats), "bars": len(obj.downbeats),
                "first_downbeat": obj.downbeats[0] if obj.downbeats else None,
                "tempo_range": [min(c["bpm"] for c in obj.tempo_curve), max(c["bpm"] for c in obj.tempo_curve)]
                if obj.tempo_curve else None}
    if isinstance(obj, Chords):
        segs = [c for c in obj.chords if c.chord != "N"]
        counts = Counter(c.chord for c in segs)
        prog = []
        for c in segs:
            if not prog or prog[-1][0] != c.chord:
                prog.append((c.chord, c.start))
        return {"backend": obj.provenance.backend, "segments": len(segs),
                "vocabulary": [k for k, _ in counts.most_common()],
                "progression": " ".join(f"{c}@{t:.1f}" for c, t in prog[:60])
                + (" …" if len(prog) > 60 else "")}
    if isinstance(obj, Lyrics):
        return {"language": obj.language, "lines": len(obj.lines),
                "words": sum(len(ln.words) for ln in obj.lines),
                "preview": [f"{ln.start:.1f}s {ln.text}" for ln in obj.lines[:12]]}
    if isinstance(obj, Score):
        tracks = []
        for t in obj.tracks:
            if not t.notes:
                continue
            ps = [n.pitch for n in t.notes]
            ms = sorted({n.measure for n in t.notes})
            tracks.append({"id": t.id, "notes": len(t.notes),
                           "range": None if t.is_drum else [midi_to_name(min(ps)), midi_to_name(max(ps))],
                           "measures": f"{ms[0]}-{ms[-1]}"})
        return {"tempo_bpm": obj.tempo_bpm, "time_signature": obj.time_signature, "key": obj.key,
                "measures": obj.measure_count, "tracks": tracks,
                "chords_by_measure": chord_line(obj), "structure_hints": repeated_progressions(obj)}
    if isinstance(obj, Arrangement):
        roles = Counter(n.role for n in obj.notes)
        frets = [n.fret for n in obj.notes if n.fret is not None]
        return {"title": obj.title, "instrument": obj.instrument, "tuning": obj.tuning, "capo": obj.capo,
                "difficulty": obj.difficulty, "key": obj.key, "tempo_bpm": obj.tempo_bpm,
                "measures": obj.measure_count, "notes": len(obj.notes), "roles": dict(roles),
                "tab_assigned": sum(1 for n in obj.notes if n.fret is not None),
                "fret_range": [min(frets), max(frets)] if frets else None,
                "sections": [s.model_dump() for s in obj.sections]}
    return {"kind": getattr(obj, "kind", None)}


def chord_line(score: Score, per_line: int = 8) -> list[str]:
    by_m: dict[int, list[str]] = {}
    for c in score.chords:
        by_m.setdefault(c.measure, []).append(c.chord if c.beat == 0 else f"{c.chord}@{_fmt(c.beat)}")
    lines = []
    last = None
    for start in range(1, score.measure_count + 1, per_line):
        cells = []
        for m in range(start, min(score.measure_count, start + per_line - 1) + 1):
            cs = by_m.get(m)
            if not cs:
                cells.append("%" if last else "-")
            else:
                cells.append(" ".join(cs))
                last = cs[-1]
        lines.append(f"m{start}: | " + " | ".join(cells) + " |")
    return lines


def repeated_progressions(score: Score, length: int = 4) -> list[dict]:
    """Find repeated 4-bar chord patterns -> candidate sections (verse/chorus)."""
    by_m = {}
    for c in score.chords:
        by_m.setdefault(c.measure, []).append(c.chord)
    sig = [tuple(by_m.get(m, [])) for m in range(1, score.measure_count + 1)]
    seen: dict[tuple, list[int]] = {}
    for i in range(0, len(sig) - length + 1):
        key = tuple(sig[i:i + length])
        if sum(1 for k in key if k) < 2:
            continue  # mostly 'chord continues' bars: not a meaningful pattern
        seen.setdefault(key, []).append(i + 1)
    out = []
    for key, starts in seen.items():
        # non-overlapping occurrences
        occ = []
        for s in starts:
            if not occ or s >= occ[-1] + length:
                occ.append(s)
        if len(occ) >= 2:
            out.append({"progression": " | ".join(" ".join(k) or "-" for k in key), "starts_at_measures": occ})
    out.sort(key=lambda d: -len(d["starts_at_measures"]))
    return out[:6]


def section(obj, measures: tuple[int, int] | None = None, start: float | None = None,
            end: float | None = None, tracks: list[str] | None = None) -> str:
    if isinstance(obj, Arrangement):
        from arpeggio.arrangement.textfmt import to_text

        return to_text(obj, measures, headers=measures is None)
    if isinstance(obj, Score):
        return _score_section(obj, measures, start, end, tracks)
    lo, hi = start or 0.0, end if end is not None else 1e9
    if isinstance(obj, Transcription):
        out = []
        for t in obj.tracks:
            if tracks and t.id not in tracks:
                continue
            ns = [n for n in t.notes if n.end > lo and n.start < hi]
            if ns:
                out.append(f"[{t.id}] " + " ".join(
                    f"{n.start:.2f}:{midi_to_name(n.pitch) if not t.is_drum else n.pitch}/{n.end - n.start:.2f}"
                    for n in ns))
        return "\n".join(out) + "\n"
    if isinstance(obj, Chords):
        return " ".join(f"{c.start:.2f}-{c.end:.2f}:{c.chord}" for c in obj.chords
                        if c.end > lo and c.start < hi) + "\n"
    if isinstance(obj, Lyrics):
        return "\n".join(f"{ln.start:.2f}-{ln.end:.2f} " + " ".join(f"{w.text}@{w.start:.2f}" for w in ln.words)
                         for ln in obj.lines if ln.end > lo and ln.start < hi) + "\n"
    if isinstance(obj, Rhythm):
        return "beats: " + " ".join(f"{b:.2f}" for b in obj.beats if lo <= b <= hi) + "\n" + \
               "downbeats: " + " ".join(f"{b:.2f}" for b in obj.downbeats if lo <= b <= hi) + "\n"
    raise ValueError("unsupported artifact")


def _score_section(score: Score, measures, start, end, tracks) -> str:
    flats = prefers_flats(score.key)
    if measures is None:
        if start is not None or end is not None:
            ms = score.measure_starts or [0.0]
            lo = 1 + sum(1 for s in ms if s <= (start or 0)) - 1 if start is not None else 1
            hi = sum(1 for s in ms if s < end) if end is not None else score.measure_count
            measures = (max(1, lo), max(1, hi))
        else:
            measures = (1, score.measure_count)
    lo, hi = measures
    out = [f"# score {score.time_signature} ♩={score.tempo_bpm} key={score.key} measures {lo}-{hi} "
           f"(beat offsets in quarters; notes: beat PITCH duration)"]
    for m in range(lo, hi + 1):
        cs = [c.chord if c.beat == 0 else f"{c.chord}@{_fmt(c.beat)}" for c in score.chords if c.measure == m]
        t0 = score.measure_starts[m - 1] if m - 1 < len(score.measure_starts) else None
        out.append(f"M {m} " + " ".join(cs) + (f"   (t={t0:.2f}s)" if t0 is not None else ""))
        for t in score.tracks:
            if tracks and t.id not in tracks:
                continue
            ns = [n for n in t.notes if n.measure == m]
            if not ns:
                continue
            body = " ".join(f"{_fmt(n.beat)}:{'x' if t.is_drum else midi_to_name(n.pitch, flats)}/{_fmt(n.duration)}"
                            for n in ns)
            out.append(f"  {t.id}: {body}")
        ws = [w for w in score.lyrics if w["measure"] == m]
        if ws:
            out.append("  lyrics: " + " ".join(f"{_fmt(w['beat'])}:{w['word']}" for w in ws))
    return "\n".join(out) + "\n"


__all__ = ["summarize", "section", "chord_line", "repeated_progressions", "name_to_midi"]
