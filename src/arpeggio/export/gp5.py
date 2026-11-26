"""Guitar Pro 5 export via PyGuitarPro."""

from __future__ import annotations

from pathlib import Path

import guitarpro as gp

from arpeggio.arrangement.instruments import MIDI_PROGRAMS, Fretboard
from arpeggio.export.events import build_events
from arpeggio.schema import Arrangement
from arpeggio.theory import key_fifths, name_to_midi


def export_gp5(arr: Arrangement, path: str | Path) -> Path:
    from arpeggio.arrangement.techniques import expand_late_vibrato

    arr = expand_late_vibrato(arr)  # delayed vibrato = plain note tied to a vibrato note
    fb = Fretboard.from_spec(arr.tuning, 0, arr.instrument)  # GP stores open tuning + capo offset
    num, _, den = arr.time_signature.partition("/")
    num, den = int(num), int(den or 4)
    measure_count, events = build_events(arr)

    song = gp.Song()
    song.title, song.artist, song.tab = arr.title, arr.artist, arr.arranger
    song.tempo = int(round(arr.tempo_bpm))
    fifths, minor = key_fifths(arr.key)
    try:
        ks = gp.KeySignature((fifths, 1 if minor else 0))
    except ValueError:
        ks = gp.KeySignature.CMajor
    song.key = ks

    song.measureHeaders = []
    start = gp.Duration.quarterTime
    for i in range(measure_count):
        ts = gp.TimeSignature(numerator=num, denominator=gp.Duration(value=den))
        h = gp.MeasureHeader(number=i + 1, start=start, timeSignature=ts, keySignature=ks)
        for s in arr.sections:
            if s.start_measure == i + 1:
                h.marker = gp.Marker(title=s.name)
        song.measureHeaders.append(h)
        start += h.length

    strings = [gp.GuitarString(number=s, value=fb.string_pitch(s)) for s in range(1, fb.n_strings + 1)]
    track = gp.Track(song, number=1, name=arr.instrument.replace("_", " ").title(), strings=strings,
                     offset=arr.capo, channel=gp.MidiChannel(channel=0, effectChannel=1,
                                                             instrument=MIDI_PROGRAMS.get(arr.instrument, 25)))
    track.fretCount = 24
    track.indicateTuning = True
    track.measures = [gp.Measure(track, h) for h in song.measureHeaders]
    song.tracks = [track]

    pieces: dict[int, list] = {}  # id(ArrNote) -> GP notes (tied pieces in order)
    for mi, measure in enumerate(track.measures):
        m = mi + 1
        for vi, v in enumerate((1, 2)):
            voice = measure.voices[vi]
            voice.direction = gp.VoiceDirection.up if v == 1 else gp.VoiceDirection.down
            evs = events[v].get(m, [])
            if v == 2 and all(not e.notes for e in evs):
                continue  # leave unused second voice empty
            for e in evs:
                dur = gp.Duration(value=e.value, isDotted=e.dotted,
                                  tuplet=gp.Tuplet(enters=3, times=2) if e.triplet else gp.Tuplet())
                beat = gp.Beat(voice, duration=dur,
                               status=gp.BeatStatus.normal if e.notes else gp.BeatStatus.rest)
                if e.chord and v == 1 or (e.chord and v == 2 and not _has_chord_v1(events, m, e)):
                    beat.text = e.chord
                used = set()
                for r in e.notes:
                    n = r.note
                    if n.string is None or n.fret is None or n.string in used:
                        continue
                    used.add(n.string)
                    note = gp.Note(beat, value=int(n.fret), string=int(n.string), velocity=gp.Velocities.forte,
                                   type=gp.NoteType.tie if r.tie_from_prev else gp.NoteType.normal)
                    pieces.setdefault(id(n), []).append(note)
                    if not r.tie_from_prev or n.tie:
                        _target_effects(note, beat, n)
                    beat.notes.append(note)
                if e.notes and not beat.notes:
                    beat.status = gp.BeatStatus.rest
                voice.beats.append(beat)
    _origin_effects(arr, pieces)
    path = Path(path)
    gp.write(song, str(path), version=(5, 1, 0))
    return path


def _target_effects(note, beat, n) -> None:
    """Effects Guitar Pro stores on the note itself."""
    from arpeggio.arrangement.techniques import tokens

    for t in tokens(n):
        if t == "vibrato":
            note.effect.vibrato = True
        elif t == "wide_vibrato":
            beat.effect.vibrato = True  # GP5 "wide vibrato" is a beat effect
        elif t in ("bend", "bend_half", "bend_release"):
            q = 2 if t == "bend_half" else 4  # GP bend points are in quarter tones
            pts = [gp.BendPoint(0, 0), gp.BendPoint(3, q), gp.BendPoint(6, q)]
            if t == "bend_release":
                pts += [gp.BendPoint(9, q), gp.BendPoint(12, 0)]
                kind = gp.BendType.bendRelease
            else:
                pts += [gp.BendPoint(12, q)]
                kind = gp.BendType.bend
            note.effect.bend = gp.BendEffect(type=kind, value=q * 25, points=pts)
        elif t == "slide_in":
            note.effect.slides = note.effect.slides + [gp.SlideType.intoFromBelow]
        elif t == "slide_out":
            note.effect.slides = note.effect.slides + [gp.SlideType.outDownwards]
        elif t == "harmonic":
            note.effect.harmonic = gp.NaturalHarmonic()
        elif t == "let_ring":
            note.effect.letRing = True
        elif t == "palm_mute":
            note.effect.palmMute = True
        elif t == "staccato":
            note.effect.staccato = True
        elif t == "ghost":
            note.effect.ghostNote = True
        elif t == "accent":
            note.effect.accentuatedNote = True


def _origin_effects(arr: Arrangement, pieces: dict) -> None:
    """Hammer-on/pull-off and legato slides are marked on the note *before* the target."""
    from arpeggio.arrangement.tablature import measure_quarters
    from arpeggio.arrangement.techniques import previous_on_string, tokens

    mlen = measure_quarters(arr.time_signature)
    for n in arr.notes:
        toks = tokens(n)
        if not any(t in ("h", "p", "slide") for t in toks):
            continue
        prev = previous_on_string(arr.notes, n, mlen)
        if prev is None or id(prev) not in pieces:
            continue
        origin = pieces[id(prev)][-1]
        if "h" in toks or "p" in toks:
            origin.effect.hammer = True
        if "slide" in toks:
            origin.effect.slides = origin.effect.slides + [gp.SlideType.legatoSlideTo]


def _has_chord_v1(events, m, e) -> bool:
    return any(x.chord == e.chord and x.onset == e.onset for x in events[1].get(m, []))


def read_back(path: str | Path) -> dict:
    """Parse a GP file and return a light summary (used to verify exports)."""
    song = gp.parse(str(path))
    t = song.tracks[0]
    notes = sum(len(b.notes) for m in t.measures for v in m.voices for b in v.beats)
    return {"title": song.title, "tempo": song.tempo, "measures": len(t.measures), "notes": notes,
            "tuning": [str(s) for s in t.strings], "capo": t.offset}


__all__ = ["export_gp5", "read_back", "name_to_midi"]
