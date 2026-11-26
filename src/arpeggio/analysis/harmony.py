"""Key estimation, note-based chord candidates and comparison with detected chords."""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from arpeggio.schema import Score
from arpeggio.theory import (
    CHORD_QUALITIES, NOTE_NAMES_FLAT, NOTE_NAMES_SHARP, estimate_key, parse_chord,
    pitch_class, prefers_flats,
)

_CANDIDATE_QUALITIES = ["", "m", "7", "m7", "maj7", "sus4", "sus2", "dim", "m7b5", "6", "add9"]

ROMAN = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"]


def chord_candidates(pc_weights: np.ndarray, bass_pc: int | None, flats: bool,
                     top: int = 3) -> list[dict]:
    names = NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP
    w = pc_weights / (pc_weights.sum() + 1e-9)
    out = []
    for q in _CANDIDATE_QUALITIES:
        ints = CHORD_QUALITIES[q]
        for root in range(12):
            tones = {(root + i) % 12 for i in ints}
            covered = sum(w[p] for p in tones)
            missing = sum(1 for p in tones if w[p] < 0.03)
            score = covered - 0.12 * missing - 0.04 * (len(tones) - 3)
            if bass_pc is not None:
                score += 0.12 if bass_pc == root else (0.04 if bass_pc in tones else -0.05)
            label = names[root] + q
            if bass_pc is not None and bass_pc != root and bass_pc in tones and q in ("", "m"):
                label_inv = f"{label}/{names[bass_pc]}"
                out.append({"chord": label_inv, "score": round(score - 0.02, 3)})
            out.append({"chord": label, "score": round(score, 3)})
    out.sort(key=lambda d: -d["score"])
    seen, res = set(), []
    for d in out:
        if d["chord"] not in seen:
            seen.add(d["chord"])
            res.append(d)
        if len(res) >= top:
            break
    return res


def roman_numeral(chord: str, key: str | None) -> str | None:
    if not key or chord in ("N", ""):
        return None
    try:
        c = parse_chord(chord)
    except ValueError:
        return None
    tonic, _, mode = key.partition(" ")
    deg = (c["root_pc"] - pitch_class(tonic)) % 12
    numeral = ROMAN[deg]
    minorish = c["quality"].startswith("m") and not c["quality"].startswith("maj")
    if minorish or c["quality"] in ("dim", "m7b5", "dim7"):
        numeral = numeral.lower()
    suffix = {"dim": "°", "m7b5": "ø7", "dim7": "°7", "7": "7", "m7": "7", "maj7": "maj7"}.get(c["quality"], "")
    return numeral + suffix


def analyze_harmony(score: Score, tracks: list[str] | None = None,
                    resolution: str = "measure") -> dict:
    bpb = score.beats_per_bar
    use = [t for t in score.tracks if not t.is_drum and (not tracks or t.id in tracks)]
    total = np.zeros(12)
    win = bpb if resolution == "measure" else max(1, bpb // 2)
    windows: dict[int, np.ndarray] = defaultdict(lambda: np.zeros(12))
    lowest: dict[int, tuple[int, float]] = {}
    for t in use:
        for n in t.notes:
            a = (n.measure - 1) * bpb + n.beat
            end = a + n.duration
            w0 = int(a // win)
            while w0 * win < end:
                seg = min(end, (w0 + 1) * win) - max(a, w0 * win)
                windows[w0][n.pitch % 12] += seg
                if seg > 0.2 and (w0 not in lowest or n.pitch < lowest[w0][0]):
                    lowest[w0] = (n.pitch, seg)
                w0 += 1
            total[n.pitch % 12] += n.duration
    keys = estimate_key(list(total))
    key = keys[0]["key"] if keys else score.key
    flats = prefers_flats(key)

    detected: dict[int, list[str]] = defaultdict(list)
    for c in score.chords:
        a = (c.measure - 1) * bpb + c.beat
        w0 = int(a // win)
        while w0 * win < a + c.duration:
            if not detected[w0] or detected[w0][-1] != c.chord:
                detected[w0].append(c.chord)
            w0 += 1

    rows = []
    agree = total_cmp = 0
    names = NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP
    for w0 in sorted(set(windows) | set(detected)):
        pcw = windows.get(w0, np.zeros(12))
        bass = lowest.get(w0, (None, 0))[0]
        cands = chord_candidates(pcw, None if bass is None else bass % 12, flats) if pcw.sum() > 0 else []
        det = detected.get(w0, [])
        row = {"measure": w0 * win // bpb + 1}
        if resolution != "measure":
            row["beat"] = (w0 * win) % bpb
        row.update({"detected": det, "from_notes": cands,
                    "bass": None if bass is None else names[bass % 12],
                    "roman": [roman_numeral(c, key) for c in det]})
        if det and cands:
            total_cmp += 1
            top_roots = {parse_chord(c["chord"].split("/")[0])["root_pc"] for c in cands[:2]}
            try:
                if parse_chord(det[0])["root_pc"] in top_roots:
                    agree += 1
                else:
                    row["disagreement"] = True
            except ValueError:
                pass
        rows.append(row)

    progression = []
    for c in score.chords:
        if not progression or progression[-1] != c.chord:
            progression.append(c.chord)
    return {"kind": "harmony", "key": key, "key_candidates": keys[:4],
            "chord_vocabulary": sorted({c.chord for c in score.chords}),
            "progression_head": progression[:32],
            "agreement_detected_vs_notes": round(agree / total_cmp, 3) if total_cmp else None,
            "windows": rows}
