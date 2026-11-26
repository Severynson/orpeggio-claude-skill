"""Arpeggio Text Format (ATF): a compact, token-efficient arrangement notation.

Example::

    @title Budemo Razom
    @instrument classical_guitar
    @tuning standard
    @capo 0
    @difficulty intermediate
    @tempo 92
    @time 4/4
    @key A minor
    @section Verse 1-8

    M 1 Am | F@2
    0    bass A2 2
    0    mel  E4:1/0 1
    1    mel  C5 .5
    1.5  harm A3,C4 .5
    2    bass F2 2 lock
    2    mel  A4:2/10 2 tech=slide

Grammar
-------
* Comments: lines starting with ``#``, or anything after ``//``.
* ``@key value`` header lines (title, artist, instrument, tuning, capo,
  difficulty, tempo, time, key, section NAME START-END, note TEXT).
* ``M <measure> [chord[@beat] ...]`` starts a measure; chords separated by
  spaces or ``|``.
* Note lines: ``<beat> <role> <pitches> <duration> [flags]``

  - role: ``mel`` | ``bass`` | ``harm`` | ``ctr`` (counter-melody) | ``perc``
  - pitches: comma separated; each ``NAME`` or ``NAME:string/fret``
    (``E4:1/0`` = E4 on string 1, open). A pitch of ``_`` with a position
    (``_:2/3``) takes its pitch from the fretboard.
  - duration in quarter notes (``.5`` = eighth, ``1.5`` = dotted quarter,
    ``0.333`` = eighth-note triplet).
  - flags: ``lock`` (keep given string/fret), ``tie``, ``v1``/``v2`` (voice),
    ``tech=<h|p|slide|harmonic|let_ring|...>``, ``f=<p|i|m|a>`` (right-hand finger).
"""

from __future__ import annotations

import re

from arpeggio.schema import ArrChord, ArrNote, Arrangement, Section
from arpeggio.theory import midi_to_name, name_to_midi, prefers_flats

ROLE_ABBR = {"mel": "melody", "bass": "bass", "harm": "harmony", "ctr": "counter",
             "perc": "percussive", "melody": "melody", "harmony": "harmony",
             "counter": "counter", "percussive": "percussive"}
ROLE_OUT = {"melody": "mel", "bass": "bass", "harmony": "harm", "counter": "ctr",
            "percussive": "perc"}


def _fmt_num(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]
    return s or "0"


def to_text(arr: Arrangement, measures: tuple[int, int] | None = None,
            headers: bool = True, positions: bool = True) -> str:
    flats = prefers_flats(arr.key)
    out = []
    if headers:
        for k in ("title", "artist", "instrument"):
            out.append(f"@{k} {getattr(arr, k)}")
        tun = arr.tuning if isinstance(arr.tuning, str) else ",".join(arr.tuning)
        out.append(f"@tuning {tun}")
        out += [f"@capo {arr.capo}", f"@difficulty {arr.difficulty}",
                f"@tempo {_fmt_num(arr.tempo_bpm)}", f"@time {arr.time_signature}"]
        if arr.key:
            out.append(f"@key {arr.key}")
        if arr.measure_count:
            out.append(f"@measures {arr.measure_count}")
        for s in arr.sections:
            out.append(f"@section {s.name} {s.start_measure}-{s.end_measure}")
        if arr.notes_text:
            for ln in arr.notes_text.splitlines():
                out.append(f"@note {ln}")
        out.append("")
    lo, hi = measures or (1, 10**9)
    by_m: dict[int, list[ArrNote]] = {}
    for n in arr.notes:
        if lo <= n.measure <= hi:
            by_m.setdefault(n.measure, []).append(n)
    chords_by_m: dict[int, list[ArrChord]] = {}
    for c in arr.chords:
        if lo <= c.measure <= hi:
            chords_by_m.setdefault(c.measure, []).append(c)
    last = max(list(by_m) + list(chords_by_m), default=lo - 1)
    for m in range(lo, min(hi, last) + 1):
        cs = sorted(chords_by_m.get(m, []), key=lambda c: c.beat)
        ctext = " ".join(c.chord if c.beat == 0 else f"{c.chord}@{_fmt_num(c.beat)}" for c in cs)
        out.append(f"M {m} {ctext}".rstrip())
        groups: dict[tuple, list[ArrNote]] = {}
        for n in sorted(by_m.get(m, []), key=lambda n: (n.beat, _role_order(n.role), _pitch(n))):
            key = (n.beat, n.role, n.duration, n.voice, n.locked, n.tie, n.technique, n.finger)
            groups.setdefault(key, []).append(n)
        for (beat, role, dur, voice, locked, tie, tech, finger), ns in groups.items():
            ps = []
            for n in ns:
                p = midi_to_name(_pitch(n), flats)
                if positions and n.string is not None and n.fret is not None:
                    p += f":{n.string}/{n.fret}"
                ps.append(p)
            flags = []
            if locked:
                flags.append("lock")
            if tie:
                flags.append("tie")
            if voice:
                flags.append(f"v{voice}")
            if tech:
                flags.append(f"tech={tech}")
            if finger:
                flags.append(f"f={finger}")
            out.append(f"{_fmt_num(beat):<5} {ROLE_OUT.get(role, role):<4} {','.join(ps)} "
                       f"{_fmt_num(dur)}" + (" " + " ".join(flags) if flags else ""))
    return "\n".join(out) + "\n"


def _role_order(role: str) -> int:
    return {"bass": 0, "harmony": 1, "counter": 2, "melody": 3}.get(role, 4)


def _pitch(n: ArrNote) -> int:
    return name_to_midi(n.pitch)


def from_text(text: str, base: Arrangement | None = None) -> Arrangement:
    """Parse ATF. Header values override `base`; notes/chords are replaced."""
    from arpeggio.arrangement.instruments import Fretboard

    data = base.model_dump() if base else {}
    data.update({"notes": [], "chords": []})
    sections = []
    note_lines = []
    measure = None
    notes, chords = [], []
    fb_cache = {}
    deferred: list[tuple[dict, int, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue  # full-line comment ('#' elsewhere is a sharp sign)
        line = raw.split("//", 1)[0].rstrip() if not raw.lstrip().startswith("@note") else raw.rstrip()
        if not line.strip():
            continue
        s = line.strip()
        try:
            if s.startswith("@"):
                key, _, val = s[1:].partition(" ")
                val = val.strip()
                if key == "section":
                    m = re.fullmatch(r"(.+?)\s+(\d+)\s*-\s*(\d+)", val)
                    if not m:
                        raise ValueError("expected '@section NAME START-END'")
                    sections.append(Section(name=m.group(1), start_measure=int(m.group(2)),
                                            end_measure=int(m.group(3))))
                elif key == "note":
                    note_lines.append(val)
                elif key in ("tempo",):
                    data["tempo_bpm"] = float(val)
                elif key == "time":
                    data["time_signature"] = val
                elif key == "capo":
                    data["capo"] = int(val)
                elif key == "measures":
                    data["measure_count"] = int(val)
                elif key == "tuning":
                    data["tuning"] = val if "," not in val else [t.strip() for t in val.split(",")]
                else:
                    data[key] = val
                continue
            if s.startswith("M ") or s == "M":
                parts = s.split()
                measure = int(parts[1])
                for tok in parts[2:]:
                    if tok == "|":
                        continue
                    ch, _, at = tok.partition("@")
                    chords.append(ArrChord(chord=ch, measure=measure, beat=float(at) if at else 0.0))
                continue
            if measure is None:
                raise ValueError("note line before any 'M <measure>' line")
            toks = s.split()
            if len(toks) < 4:
                raise ValueError("expected '<beat> <role> <pitches> <duration> [flags]'")
            beat, role, pitches, dur = float(toks[0]), ROLE_ABBR.get(toks[1]), toks[2], float(toks[3])
            if role is None:
                raise ValueError(f"unknown role '{toks[1]}'")
            flags = {"locked": False, "tie": False}
            for f in toks[4:]:
                if f == "lock":
                    flags["locked"] = True
                elif f == "tie":
                    flags["tie"] = True
                elif re.fullmatch(r"v[12]", f):
                    flags["voice"] = int(f[1])
                elif f.startswith("tech="):
                    flags["technique"] = f[5:]
                elif f.startswith("f="):
                    flags["finger"] = f[2:]
                else:
                    raise ValueError(f"unknown flag '{f}'")
            for p in pitches.split(","):
                name, _, pos = p.partition(":")
                string = fret = None
                if pos:
                    a, _, b = pos.partition("/")
                    string, fret = int(a), int(b)
                rec = dict(measure=measure, beat=beat, duration=dur, role=role, string=string,
                           fret=fret, **flags)
                if name == "_":
                    if string is None:
                        raise ValueError("'_' pitch needs a :string/fret position")
                    deferred.append((rec, string, fret))
                    continue
                rec["pitch"] = name_to_midi(name)
                notes.append(rec)
        except Exception as e:
            raise ValueError(f"ATF line {lineno}: {e}: {raw!r}") from e
    if deferred:
        fb = fb_cache.setdefault("fb", Fretboard.from_spec(data.get("tuning", "standard"),
                                                           int(data.get("capo", 0)),
                                                           data.get("instrument", "acoustic_guitar")))
        for rec, s_, f_ in deferred:
            rec["pitch"] = fb.pitch_at(s_, f_)
            notes.append(rec)
    data["notes"] = [ArrNote(**n) for n in notes]
    data["chords"] = chords
    if sections:
        data["sections"] = sections
    if note_lines:
        data["notes_text"] = "\n".join(note_lines)
    data.setdefault("kind", "arrangement")
    arr = Arrangement.model_validate(data)
    if not arr.measure_count and arr.notes:
        arr.measure_count = max(n.measure for n in arr.notes)
    return arr


def patch(arr: Arrangement, section: Arrangement) -> Arrangement:
    """Replace the measures present in `section` (notes + chords)."""
    ms = {n.measure for n in section.notes} | {c.measure for c in section.chords}
    out = arr.model_copy(deep=True)
    out.notes = [n for n in out.notes if n.measure not in ms] + section.notes
    out.chords = [c for c in out.chords if c.measure not in ms] + section.chords
    out.notes.sort(key=lambda n: (n.measure, n.beat))
    out.chords.sort(key=lambda c: (c.measure, c.beat))
    out.measure_count = max([out.measure_count or 0] + [n.measure for n in out.notes])
    return out
