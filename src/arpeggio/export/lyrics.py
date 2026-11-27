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
    """(starting measure, text) of the first Guitar Pro lyric line (see gp_lyrics_lines)."""
    lines = gp_lyrics_lines(sl, assign)
    return lines[0] if lines else (1, "")


def gp_lyrics_lines(sl, assign, max_lines: int = 5) -> list[tuple[int, str]]:
    """Guitar Pro lyric lines [(starting bar, text)], at most 5.

    Players disagree on tied beats (Guitar Pro gives them a syllable, Songsterr
    skips them), so a new line — which restarts the count at its own bar — begins
    wherever a tied beat lies between two words. Inside a line there are then
    only struck beats and rests, which every player counts the same way."""
    if not assign:
        return []
    keys = sorted(assign)
    groups = [[keys[0]]]
    for a, b in zip(keys, keys[1:]):
        if any(not sl[k][4] for k in range(a + 1, b)):
            groups.append([b])
        else:
            groups[-1].append(b)
    while len(groups) > max_lines:  # merge the pair with the fewest words (ambiguity stays there)
        i = min(range(len(groups) - 1), key=lambda i: len(groups[i]) + len(groups[i + 1]))
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
    out = []
    for g in groups:
        start_m = sl[g[0]][1]
        k0 = next(k for k, s in enumerate(sl) if s[1] == start_m)  # first beat of that bar
        parts = [slot_text(assign[k]) if k in assign else "" for k in range(k0, g[-1] + 1)]
        out.append((start_m, " ".join(parts)))
    return out


def anchor_voice1(arr, tol: float = 0.375, reach: float = 0.6):
    """Guitar Pro can hang a syllable only on a struck beat of voice 1. Where the
    melody voice holds a note through a sung word, move the voice-2 notes struck
    at that moment into voice 1 (same strings/frets/timing). Notes that voice 1
    now cuts short get `let_ring`, so playback still sustains them. No ties are
    introduced. Returns (arrangement copy, number of promoted onsets)."""
    from arpeggio.arrangement.tablature import abs_beat, measure_quarters
    from arpeggio.arrangement.techniques import add_token
    from arpeggio.export.events import voice_of

    if not arr.lyrics:
        return arr, 0
    mlen = measure_quarters(arr.time_signature)
    out = arr.model_copy(deep=True)
    orig_voice = {id(n): voice_of(n) for n in out.notes}
    v1 = sorted({round(abs_beat(n, mlen), 4) for n in out.notes if voice_of(n) == 1 and not n.tie})
    v2: dict[float, list] = {}
    for n in out.notes:
        if voice_of(n) == 2 and not n.tie and n.string is not None:
            v2.setdefault(round(abs_beat(n, mlen), 4), []).append(n)
    promoted = 0
    for w in sorted(arr.lyrics, key=lambda w: (w.measure, w.beat)):
        t = (w.measure - 1) * mlen + w.beat
        if any(abs(a - t) <= tol for a in v1):
            continue
        cands = [a for a in v2 if abs(a - t) <= reach and a not in v1]
        if not cands:
            continue
        a = min(cands, key=lambda a: (abs(a - t), a > t))
        for n in v2.pop(a):
            n.voice = 1
        v1 = sorted(v1 + [a])
        promoted += 1
    if promoted:
        def cut(n, voice):
            s0, e0 = abs_beat(n, mlen), abs_beat(n, mlen) + n.duration
            return any(s0 + 1e-6 < abs_beat(m, mlen) < e0 - 1e-6
                       for m in out.notes if m is not n and not m.tie and
                       (voice_of(m) if voice == "now" else orig_voice[id(m)]) == (
                           voice_of(n) if voice == "now" else orig_voice[id(n)]))
        for n in out.notes:
            if n.string is not None and cut(n, "now") and not cut(n, "before"):
                add_token(n, "let_ring")
    return out, promoted


def gp_plan(arr, lyrics_track: bool = False):
    """Everything the GP5 writer needs for lyrics: (arrangement to write, slots,
    assignment, lines, stats). Guitar mode anchors voice 1 first."""
    from fractions import Fraction as F

    from arpeggio.export.events import build_events

    num, _, den = arr.time_signature.partition("/")
    mlen = F(int(num) * 4, int(den or 4))
    promoted = 0
    if lyrics_track:
        from arpeggio.export.gp5 import _rhythm_arrangement

        target = _rhythm_arrangement(arr)
        write = arr
    else:
        write, promoted = anchor_voice1(arr)
        target = write
    sl, assign, stats = place(target, build_events(target)[1], mlen)
    lines = gp_lyrics_lines(sl, assign)
    stats = {**stats, "lyric_lines": len(lines), "melody_voice_anchors": promoted}
    return write, sl, assign, lines, stats


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
