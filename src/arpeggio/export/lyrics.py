"""Place an arrangement's lyrics on notation beats, shared by GP5 / MusicXML / PDF.

Guitar Pro assigns one syllable per beat of the lyric track's first voice,
starting at the line's bar and skipping rests; a blank beat is an extra space
(semantics as implemented by alphaTab's GP importer, which mirrors Guitar Pro;
tied-note beats DO take a syllable there, so they get blanks). MuseScore's GP5
lyric import is not a reference: it misreads the start bar and track fields.
We compute the same "slots" and put each word on the struck slot nearest to its
sung onset, keeping order; words that fall on one slot are joined.
"""

from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction

from arpeggio.export.events import Event

NBSP = " "


def slots(events: dict[int, dict[int, list[Event]]], mlen: Fraction,
          voices: tuple[int, ...] = (1,), count_ties: bool = False) -> list[tuple]:
    """[(absolute onset, measure, index in events[voice][measure], voice, struck)] for
    note events (tied-only events too when `count_ties`, as Guitar Pro counts them). Guitar Pro: voice 1 only. MusicXML can hang a lyric on any
    note of the staff, so it uses (1, 2): voice 1 where both strike together."""
    best: dict[Fraction, tuple] = {}
    for v in voices:
        for m in sorted(events.get(v, {})):
            for k, e in enumerate(events[v][m]):
                if not e.notes:
                    continue
                struck = any(not r.tie_from_prev for r in e.notes)
                if not struck and not count_ties:
                    continue
                a = (m - 1) * mlen + e.onset
                if a not in best or (struck and not best[a][4]):
                    best[a] = (a, m, k, v, struck)
    return [best[a] for a in sorted(best)]


def place(arr, events, mlen: Fraction, voices: tuple[int, ...] = (1,), count_ties: bool | None = None):
    """Returns (slots, {slot index: [ArrLyric,...]}, stats). Guitar Pro mode
    (voice 1 only) counts tied beats as blank slots."""
    if count_ties is None:
        count_ties = voices == (1,)
    sl = slots(events, mlen, voices, count_ties)
    assign: dict[int, list] = {}
    if not sl or not arr.lyrics:
        return sl, assign, {"words": 0}
    usable = [i for i, x in enumerate(sl) if x[4]]
    onsets = [float(sl[i][0]) for i in usable]
    prev = -1
    shifts = []
    for w in sorted(arr.lyrics, key=lambda x: (x.measure, x.beat)):
        t = (w.measure - 1) * float(mlen) + w.beat
        j = bisect_left(onsets, t)
        cands = [c for c in (j - 1, j) if 0 <= c < len(onsets)]
        j = min(cands, key=lambda c: abs(onsets[c] - t))
        if j < prev:
            j = prev
        if j == prev and j + 1 < len(onsets) and onsets[j + 1] - t <= 1.0:
            j += 1  # next slot is close enough: don't cram two words onto one beat
        assign.setdefault(usable[j], []).append(w)
        shifts.append(abs(onsets[j] - t))
        prev = j
    joined = sum(1 for v in assign.values() if len(v) > 1)
    return sl, assign, {"words": len(shifts), "slots_used": len(assign), "joined_slots": joined,
                        "mean_shift_beats": round(sum(shifts) / len(shifts), 3),
                        "shifted_over_1_beat": sum(1 for s in shifts if s > 1.0)}


def clean_word(text: str) -> str:
    """Safe for Guitar Pro's lyric syntax: no separators or [comments] inside a word."""
    return (text.replace("-", "–").replace("+", "–").replace("[", "(").replace("]", ")")
            .replace(" ", NBSP).strip())


def slot_text(words) -> str:
    return NBSP.join(clean_word(w.text) for w in words)


def gp_lyrics_line(sl, assign) -> tuple[int, str]:
    """(starting measure, text) for one Guitar Pro lyric line covering all words."""
    if not assign:
        return 1, ""
    first, last = min(assign), max(assign)
    start_m = sl[first][1]
    k0 = next(k for k, s in enumerate(sl) if s[1] == start_m)  # first slot of that measure
    parts = [slot_text(assign[k]) if k in assign else "" for k in range(k0, last + 1)]
    return start_m, " ".join(parts)


def gp_encoding(texts: list[str], requested: str = "auto") -> str:
    """GP5 stores 8-bit text with no charset marker (Guitar Pro uses the system
    code page). auto: cp1252 if it fits, else the first code page that does."""
    if requested != "auto":
        return requested
    blob = "".join(texts)
    for enc in ("cp1252", "cp1251", "cp1250", "cp1253", "cp1254", "cp1257"):
        try:
            blob.encode(enc)
            return enc
        except UnicodeEncodeError:
            continue
    return "utf-8"
