"""Turn an arrangement into notation events: per measure, per voice, with rests and ties.

Shared by the Guitar Pro, MusicXML and ASCII exporters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from arpeggio.arrangement.tablature import measure_quarters
from arpeggio.schema import ArrNote, Arrangement
from arpeggio.theory import name_to_midi

GRID = Fraction(1, 24)  # quarter / 24 covers 32nds and 16th-triplets

# Representable durations (quarters) -> (type value: 1=whole..32, dotted, triplet)
NOTE_VALUES: list[tuple[Fraction, int, bool, bool]] = [
    (Fraction(4), 1, False, False),
    (Fraction(3), 2, True, False),
    (Fraction(2), 2, False, False),
    (Fraction(3, 2), 4, True, False),
    (Fraction(4, 3), 2, False, True),
    (Fraction(1), 4, False, False),
    (Fraction(3, 4), 8, True, False),
    (Fraction(2, 3), 4, False, True),
    (Fraction(1, 2), 8, False, False),
    (Fraction(3, 8), 16, True, False),
    (Fraction(1, 3), 8, False, True),
    (Fraction(1, 4), 16, False, False),
    (Fraction(1, 6), 16, False, True),
    (Fraction(1, 8), 32, False, False),
    (Fraction(1, 12), 32, False, True),
]


def snap(x: float) -> Fraction:
    return Fraction(round(Fraction(x) / GRID)) * GRID


def decompose(d: Fraction) -> list[tuple[Fraction, int, bool, bool]]:
    out = []
    rem = d
    while rem > 0:
        for v in NOTE_VALUES:
            if v[0] <= rem:
                out.append(v)
                rem -= v[0]
                break
        else:  # smaller than the smallest value: absorb
            break
    return out


@dataclass
class NoteRef:
    note: ArrNote
    pitch: int
    tie_from_prev: bool = False
    tie_to_next: bool = False


@dataclass
class Event:
    onset: Fraction  # within measure
    duration: Fraction
    value: int
    dotted: bool
    triplet: bool
    notes: list[NoteRef] = field(default_factory=list)  # empty = rest
    chord: str | None = None


def voice_of(n: ArrNote) -> int:
    if n.voice in (1, 2):
        return n.voice
    return 2 if n.role in ("bass", "harmony") else 1


def build_events(arr: Arrangement) -> tuple[int, dict[int, dict[int, list[Event]]]]:
    """Return (measure_count, {voice: {measure: [Event]}})."""
    mlen = snap(measure_quarters(arr.time_signature))
    measure_count = max([arr.measure_count or 0] + [n.measure for n in arr.notes] +
                        [c.measure for c in arr.chords] + [1])
    chord_at = {(c.measure, snap(c.beat)): c.chord for c in arr.chords}
    result: dict[int, dict[int, list[Event]]] = {}
    for v in (1, 2):
        vnotes = [n for n in arr.notes if voice_of(n) == v]
        # absolute onsets
        groups: dict[Fraction, list[ArrNote]] = {}
        for n in vnotes:
            a = (n.measure - 1) * mlen + snap(n.beat)
            groups.setdefault(a, []).append(n)
        onsets = sorted(groups)
        # segments: (abs_start, abs_end, notes)
        segs = []
        for i, a in enumerate(onsets):
            nxt = onsets[i + 1] if i + 1 < len(onsets) else None
            longest = max(max(GRID, snap(n.duration)) for n in groups[a])
            end = a + longest
            if nxt is not None and nxt < end:
                end = nxt
            segs.append((a, end, groups[a]))
        per_measure: dict[int, list[Event]] = {m: [] for m in range(1, measure_count + 1)}
        cursor = Fraction(0)
        for a, end, ns in segs:
            if a > cursor:
                _emit(per_measure, cursor, a, [], mlen, measure_count, chord_at)
            _emit(per_measure, a, end, ns, mlen, measure_count, chord_at)
            cursor = end
        total = mlen * measure_count
        if cursor < total:
            _emit(per_measure, cursor, total, [], mlen, measure_count, chord_at)
        result[v] = per_measure
    # chords on beats without a voice-1 onset: attach to whichever voice has an event there
    return measure_count, result


def _emit(per_measure, a: Fraction, b: Fraction, notes: list[ArrNote], mlen: Fraction,
          measure_count: int, chord_at) -> None:
    first = True
    t = a
    while t < b:
        m = int(t // mlen) + 1
        if m > measure_count:
            return
        m_end = m * mlen
        seg_end = min(b, m_end)
        onset_in = t - (m - 1) * mlen
        pieces = decompose(seg_end - t)
        for k, (d, val, dot, trip) in enumerate(pieces):
            is_first_piece = first and k == 0
            last_piece = (seg_end == b) and k == len(pieces) - 1
            refs = [NoteRef(n, name_to_midi(n.pitch), tie_from_prev=(not is_first_piece) or n.tie,
                            tie_to_next=not last_piece) for n in notes]
            ev = Event(onset=onset_in, duration=d, value=val, dotted=dot, triplet=trip, notes=refs,
                       chord=chord_at.get((m, onset_in)))
            per_measure[m].append(ev)
            onset_in += d
        first = False
        t = seg_end
