"""Guitar Pro 5 export via PyGuitarPro."""

from __future__ import annotations

from pathlib import Path

import guitarpro as gp

from arpeggio.arrangement.instruments import MIDI_PROGRAMS, Fretboard
from arpeggio.export.events import build_events
from arpeggio.schema import Arrangement
from arpeggio.theory import key_fifths, name_to_midi


def export_gp5(arr: Arrangement, path: str | Path, lyrics: bool = True, encoding: str = "auto",
               lyrics_track: bool = False) -> Path:
    """Guitar Pro 5 file. `lyrics`: write the arrangement's lyrics as the track's
    lyric line, one word per struck voice-1 beat. `encoding`: 8-bit code page for
    all text (auto = cp1252, or e.g. cp1251 when the text is Cyrillic).
    `lyrics_track`: put the lyrics on an extra muted "Lyrics" track with one
    rhythm note (x) per sung word — exact timing, shown in multitrack view —
    instead of on the guitar's melody voice (approximate where it holds notes)."""
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
    texts = [arr.title, arr.artist, arr.arranger] + [s.name for s in arr.sections]
    if lyrics and arr.lyrics:
        from fractions import Fraction as _F

        from arpeggio.export.lyrics import gp_lyrics_line, place

        if lyrics_track:
            song.tracks.append(_lyrics_track(song, arr, fb))
            sub = _rhythm_arrangement(arr)
            sl, assign, _ = place(sub, build_events(sub)[1], _F(num * 4, den))
        else:
            sl, assign, _ = place(arr, events, _F(num * 4, den))
        start_m, text = gp_lyrics_line(sl, assign)
        song.lyrics = gp.Lyrics(trackChoice=len(song.tracks) if lyrics_track else 1,
                                lines=[gp.LyricLine(start_m, text)] + [gp.LyricLine(1, "") for _ in range(4)])
        texts.append(text)
    from arpeggio.export.lyrics import gp_encoding

    path = Path(path)
    enc = gp_encoding(texts, encoding)
    try:
        gp.write(song, str(path), version=(5, 1, 0), encoding=enc)
    except UnicodeEncodeError as e:
        from arpeggio.artifacts import ArpeggioError

        raise ArpeggioError(f"GP5 text cannot be written as {enc} ({e.object[e.start:e.end]!r}): use "
                            "--gp5-encoding auto (or a code page with these letters, e.g. cp1251 for "
                            "Cyrillic), or export --no-lyrics") from e
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


def _rhythm_arrangement(arr: Arrangement) -> Arrangement:
    """One note per sung word (G4 on string 1), lasting until the next word or 2 beats."""
    from arpeggio.arrangement.tablature import measure_quarters
    from arpeggio.schema import ArrNote

    mlen = measure_quarters(arr.time_signature)
    ws = sorted(arr.lyrics, key=lambda w: (w.measure, w.beat))
    notes = []
    for i, w in enumerate(ws):
        beat = round(w.beat * 4) / 4  # sixteenth grid
        t = (w.measure - 1) * mlen + beat
        if notes and t <= (notes[-1].measure - 1) * mlen + notes[-1].beat:
            continue  # two words on one sixteenth: place() joins them on the earlier note
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        tn = ((nxt.measure - 1) * mlen + round(nxt.beat * 4) / 4) if nxt else t + 2
        m = int(t // mlen) + 1
        notes.append(ArrNote(pitch="G4", measure=m, beat=t - (m - 1) * mlen, duration=max(0.25, min(2.0, tn - t)),
                             role="melody", voice=1, string=1, fret=3))
    return arr.model_copy(update={"notes": notes, "chords": []})


def _lyrics_track(song, arr: Arrangement, fb) -> "gp.Track":
    sub = _rhythm_arrangement(arr)
    _, events = build_events(sub)
    strings = [gp.GuitarString(number=s, value=fb.string_pitch(s)) for s in range(1, fb.n_strings + 1)]
    track = gp.Track(song, number=len(song.tracks) + 1, name="Lyrics", strings=strings,
                     channel=gp.MidiChannel(channel=2, effectChannel=3, instrument=54, volume=0))
    track.measures = [gp.Measure(track, h) for h in song.measureHeaders]
    for mi, measure in enumerate(track.measures):
        voice = measure.voices[0]
        for e in events[1].get(mi + 1, []):
            dur = gp.Duration(value=e.value, isDotted=e.dotted,
                              tuplet=gp.Tuplet(enters=3, times=2) if e.triplet else gp.Tuplet())
            beat = gp.Beat(voice, duration=dur, status=gp.BeatStatus.normal if e.notes else gp.BeatStatus.rest)
            if e.notes:
                tie = any(r.tie_from_prev for r in e.notes)
                beat.notes.append(gp.Note(beat, value=3, string=1, velocity=gp.Velocities.pianoPianissimo,
                                          type=gp.NoteType.tie if tie else gp.NoteType.dead))
            voice.beats.append(beat)
    return track


def read_back(path: str | Path, encoding: str = "cp1252") -> dict:
    """Parse a GP file and return a light summary (used to verify exports)."""
    song = gp.parse(str(path), encoding=encoding)
    t = song.tracks[0]
    notes = sum(len(b.notes) for m in t.measures for v in m.voices for b in v.beats)
    out = {"title": song.title, "tempo": song.tempo, "measures": len(t.measures), "notes": notes,
           "tuning": [str(s) for s in t.strings], "capo": t.offset}
    line = song.lyrics.lines[0] if song.lyrics and song.lyrics.lines else None
    if line is not None and line.lyrics:
        out["lyrics"] = {"start_measure": line.startingMeasure, "words": len(line.lyrics.split()),
                         "encoding": encoding}
    return out


__all__ = ["export_gp5", "read_back", "name_to_midi"]
