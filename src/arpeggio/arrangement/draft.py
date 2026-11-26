"""Deterministic baseline fingerstyle arrangement.

This is a *starting point* for the agent, not a finished arrangement: it places
the melody on top, chord roots (or transcribed bass notes) underneath, and
optional chord-tone fills in a chosen accompaniment pattern. The agent is
expected to review it section by section, fix wrong notes, shape the rhythm
and voice-leading, then optimise and validate the tablature.
"""

from __future__ import annotations

import statistics

from arpeggio.arrangement.instruments import DIFFICULTIES, Fretboard
from arpeggio.schema import ArrChord, ArrNote, Arrangement, QChord, Score, Section
from arpeggio.theory import key_name, parse_chord, pitch_class, transpose_chord

GUITAR_FRIENDLY_KEYS = {
    "C major": 1.0, "G major": 1.0, "D major": 1.0, "A major": 0.9, "E major": 0.9,
    "F major": 0.4, "A minor": 1.0, "E minor": 1.0, "D minor": 0.85, "B minor": 0.5,
    "F# minor": 0.4, "C# minor": 0.3, "G minor": 0.3, "B major": 0.2, "Bb major": 0.2,
}

STYLES = {
    # beats (quarter offsets) where the bass plays, per beats-per-bar
    "ballad": {"bass": {4: [0, 2], 3: [0], 2: [0], 6: [0, 3]}, "fill": [], "fill_step": None},
    "arpeggio": {"bass": {4: [0, 2], 3: [0], 2: [0], 6: [0, 3]}, "fill": "arp", "fill_step": 0.5},
    "travis": {"bass": {4: [0, 1, 2, 3], 3: [0, 1, 2], 2: [0, 1], 6: [0, 1.5, 3, 4.5]},
               "fill": "offbeat", "fill_step": 0.5},
    "sparse": {"bass": {4: [0], 3: [0], 2: [0], 6: [0]}, "fill": [], "fill_step": None},
}


def best_transposition(score: Score, melody_pitches: list[int], allow: range = range(-5, 7)) -> dict:
    """Score each transposition by key friendliness and melody fit on guitar."""
    key = score.key or "C major"
    tonic, _, mode = key.partition(" ")
    tpc = pitch_class(tonic)
    rows = []
    for k in allow:
        name = key_name((tpc + k) % 12, mode)
        friendly = GUITAR_FRIENDLY_KEYS.get(name, 0.05)
        rows.append({"semitones": k, "key": name, "score": round(friendly - 0.03 * abs(k), 3)})
    rows.sort(key=lambda r: -r["score"])
    return {"original_key": key, "ranked": rows[:5]}


def _octave_fit(pitches: list[int], lo: int, hi: int, center: int) -> int:
    """Octave shift (multiple of 12) bringing most pitches into [lo, hi]."""
    if not pitches:
        return 0
    best = None
    med = statistics.median(pitches)
    for k in range(-4, 5):
        s = 12 * k
        inside = sum(1 for p in pitches if lo <= p + s <= hi)
        key = (inside, -abs(med + s - center))
        if best is None or key > best[0]:
            best = (key, s)
    return best[1]


def _chord_at(chords: list[QChord], a: float, bpb: int) -> QChord | None:
    cur = None
    for c in chords:
        ca = (c.measure - 1) * bpb + c.beat
        if ca <= a + 1e-6:
            cur = c
        else:
            break
    if cur is not None and (cur.measure - 1) * bpb + cur.beat + cur.duration + bpb < a:
        return None
    return cur


def draft_arrangement(score: Score, style: str = "arpeggio", difficulty: str = "intermediate",
                      instrument: str = "classical_guitar", tuning: str | list[str] = "standard",
                      capo: int = 0, transpose: int | str = 0, title: str = "Untitled",
                      artist: str = "", measures: tuple[int, int] | None = None) -> tuple[Arrangement, dict]:
    if style not in STYLES:
        raise ValueError(f"unknown style '{style}'. Choose from {', '.join(STYLES)}")
    st = STYLES[style]
    bpb = score.beats_per_bar
    tracks = {t.id: t for t in score.tracks}
    melody = list(tracks["melody"].notes) if "melody" in tracks else []
    bass_line = list(tracks["bass"].notes) if "bass" in tracks else []
    lo_m, hi_m = measures or (1, score.measure_count)
    melody = [n for n in melody if lo_m <= n.measure <= hi_m]
    bass_line = [n for n in bass_line if lo_m <= n.measure <= hi_m]
    chords = sorted([c for c in score.chords if lo_m <= c.measure <= hi_m],
                    key=lambda c: (c.measure, c.beat))
    info: dict = {}

    if transpose == "auto":
        tr = best_transposition(score, [n.pitch for n in melody])
        semis = tr["ranked"][0]["semitones"]
        info["transposition"] = tr
    else:
        semis = int(transpose)
    key = score.key
    if semis and key:
        tonic, _, mode = key.partition(" ")
        key = key_name((pitch_class(tonic) + semis) % 12, mode)

    fb = Fretboard.from_spec(tuning, capo, instrument)
    # Melody: sounding range on guitar roughly B3..B5, centred around G4/A4
    mel_p = [n.pitch + semis for n in melody]
    mel_shift = _octave_fit(mel_p, max(fb.lowest + 12, 55), fb.open_midi_low_to_high[-1] + 12, 67)
    info["melody_octave_shift"] = mel_shift // 12

    notes: list[ArrNote] = []
    mel_onsets: dict[float, int] = {}
    mel_top = fb.open_midi_low_to_high[-1] + DIFFICULTIES[difficulty].max_fret
    for n in melody:
        p = n.pitch + semis + mel_shift
        while p < fb.lowest + 12:
            p += 12
        while p > mel_top:
            p -= 12
        a = (n.measure - 1) * bpb + n.beat
        notes.append(ArrNote(pitch=p, measure=n.measure, beat=n.beat, duration=n.duration,
                             role="melody", voice=1))
        mel_onsets[round(a, 4)] = p

    def mel_ceiling(a: float) -> int:
        """Highest pitch the accompaniment may use at time a (below the melody)."""
        sounding = [mp for n, mp in zip(melody, [x.pitch for x in notes if x.role == "melody"])
                    if (n.measure - 1) * bpb + n.beat <= a + 1e-6 < (n.measure - 1) * bpb + n.beat + n.duration]
        if sounding:
            return min(sounding) - 3
        nxt = [mp for t_, mp in sorted(mel_onsets.items()) if a < t_ <= a + 2]
        return max(nxt[0] - 3, low + 17) if nxt else 64

    # Bass: one note per pattern beat, chord root in E2..D3, transcribed bass if it's a chord tone
    pattern = st["bass"].get(bpb, [0])
    bass_by_onset = {round((n.measure - 1) * bpb + n.beat, 3): n.pitch + semis for n in bass_line}
    out_chords: list[ArrChord] = []
    for c in chords:
        out_chords.append(ArrChord(chord=transpose_chord(c.chord, semis) if semis else c.chord,
                                   measure=c.measure, beat=c.beat))
    tchords = [QChord(chord=ac.chord, measure=ac.measure, beat=ac.beat, duration=c.duration)
               for ac, c in zip(out_chords, chords)]

    low = fb.lowest
    for m in range(lo_m, hi_m + 1):
        bar_start = (m - 1) * bpb
        hits = sorted(set(pattern) | {c.beat for c in tchords if c.measure == m and c.beat % 1 == 0})
        for i, b in enumerate(hits):
            a = bar_start + b
            ch = _chord_at(tchords, a, bpb)
            if ch is None:
                continue
            try:
                pc = parse_chord(ch.chord)
            except ValueError:
                continue
            nxt = hits[i + 1] if i + 1 < len(hits) else bpb
            dur = nxt - b
            root = pc.get("bass_pc", pc["root_pc"])
            # alternating bass: fifth on off-pattern beats for travis style
            if style == "travis" and i % 2 == 1:
                root = (pc["root_pc"] + 7) % 12
            target = None
            near = [p for t_, p in bass_by_onset.items() if abs(t_ - a) < 0.3]
            if near and near[0] % 12 in pc["pitch_classes"]:
                target = near[0]
            p = target if target is not None else root + 12 * ((low - root) // 12 + 1)
            while p < low:
                p += 12
            while p > low + 14:
                p -= 12
            notes.append(ArrNote(pitch=p, measure=m, beat=b, duration=dur, role="bass", voice=2))

        # Fills
        if st["fill"]:
            step = st["fill_step"]
            t = 0.0
            k = 0
            while t < bpb - 1e-6:
                a = bar_start + t
                is_bass_beat = any(abs(t - hb) < 1e-6 for hb in hits)
                has_mel = round(a, 4) in mel_onsets
                if not is_bass_beat and not has_mel:
                    ch = _chord_at(tchords, a, bpb)
                    if ch:
                        try:
                            pc = parse_chord(ch.chord)
                        except ValueError:
                            pc = None
                        if pc:
                            ceiling = mel_ceiling(a)
                            tones = set(pc["pitch_classes"])
                            # fills live in the middle register: above the bass, below the melody
                            cand = [p for p in range(low + 10, min(ceiling, low + 26) + 1)
                                    if p % 12 in tones]
                            if cand:
                                if st["fill"] == "arp":  # rising-falling arpeggio through chord tones
                                    cycle = list(range(len(cand))) + list(range(len(cand) - 2, 0, -1))
                                    choice = cand[cycle[k % len(cycle)]]
                                else:  # travis: highest chord tone on the off-beat
                                    choice = cand[-1]
                                notes.append(ArrNote(pitch=choice, measure=m, beat=round(t, 4),
                                                     duration=step, role="harmony", voice=2))
                                k += 1
                t += step

    arr = Arrangement(title=title, artist=artist, instrument=instrument, tuning=tuning, capo=capo,
                      difficulty=difficulty, tempo_bpm=score.tempo_bpm,
                      time_signature=score.time_signature, key=key,
                      measure_count=hi_m, chords=out_chords, notes=notes,
                      sections=[Section(name="All", start_measure=lo_m, end_measure=hi_m)],
                      notes_text=f"Draft generated with style={style}; review melody, bass and "
                                 "fills section by section before exporting.")
    arr.notes.sort(key=lambda n: (n.measure, n.beat, n.role != "bass"))
    info.update({"semitones": semis, "key": key, "notes": len(notes),
                 "melody_notes": sum(1 for n in notes if n.role == "melody"),
                 "bass_notes": sum(1 for n in notes if n.role == "bass"),
                 "fill_notes": sum(1 for n in notes if n.role == "harmony")})
    return arr, info


def transpose_arrangement(arr: Arrangement, semitones: int) -> Arrangement:
    from arpeggio.theory import name_to_midi

    out = arr.model_copy(deep=True)
    for n in out.notes:
        n.pitch = name_to_midi(n.pitch) + semitones
        n.string = n.fret = None
        n.locked = False
    out.chords = [ArrChord(chord=transpose_chord(c.chord, semitones), measure=c.measure, beat=c.beat)
                  for c in out.chords]
    if out.key:
        tonic, _, mode = out.key.partition(" ")
        out.key = key_name((pitch_class(tonic) + semitones) % 12, mode)
    return out
