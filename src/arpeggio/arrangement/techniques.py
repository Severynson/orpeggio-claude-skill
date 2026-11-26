"""Expressive techniques: vocabulary, suggestions and sanity checks.

A note's `technique` field holds one or more tokens joined by "+"
(e.g. "slide+vibrato"). Tokens describe how *this* note is produced:

* vibrato, wide_vibrato    — pitch wobble on a held fretted note
* bend, bend_half, bend_release — string bend up a whole / half tone (release = back down)
* h, p                     — hammer-on / pull-off from the previous note on the same string
* slide                    — slide from the previous note on the same string into this one
* slide_in, slide_out      — slide into the note from below / out of it downwards
* harmonic                 — natural harmonic
* let_ring, palm_mute, staccato, ghost, accent

Suggestions never change pitches or rhythm: they only propose tokens, each with
a reason and a confidence, so the arranger (Claude) can accept or reject them.
"""

from __future__ import annotations

from collections import defaultdict

from arpeggio.arrangement.instruments import DIFFICULTIES
from arpeggio.arrangement.tablature import abs_beat, measure_quarters
from arpeggio.schema import ArrNote, Arrangement
from arpeggio.theory import midi_to_name, name_to_midi

TOKENS = {
    "vibrato", "wide_vibrato", "bend", "bend_half", "bend_release", "h", "p", "slide",
    "slide_in", "slide_out", "harmonic", "let_ring", "palm_mute", "staccato", "ghost", "accent",
}
ALIASES = {"hammer": "h", "pull": "p", "nh": "harmonic", "s": "slide", "~": "vibrato",
           "pm": "palm_mute", "vib": "vibrato", "b": "bend"}
PITCH_EFFECTS = {"vibrato", "wide_vibrato", "bend", "bend_half", "bend_release"}
LEGATO = {"h", "p", "slide"}


def tokens(n: ArrNote) -> list[str]:
    if not n.technique:
        return []
    out = []
    for t in str(n.technique).replace(",", "+").split("+"):
        t = t.strip().lower()
        if t:
            out.append(ALIASES.get(t, t))
    return out


def has(n: ArrNote, tok: str) -> bool:
    return tok in tokens(n)


def add_token(n: ArrNote, tok: str) -> None:
    cur = tokens(n)
    if tok not in cur:
        n.technique = "+".join(cur + [tok])


def previous_on_string(notes: list[ArrNote], n: ArrNote, mlen: float) -> ArrNote | None:
    """The latest earlier note on the same string (assigned positions only)."""
    t = abs_beat(n, mlen)
    best = None
    for m in notes:
        if m is n or m.string != n.string or m.string is None:
            continue
        tm = abs_beat(m, mlen)
        if tm < t - 1e-6 and (best is None or tm > abs_beat(best, mlen)):
            best = m
    return best


# ---------------------------------------------------------------------------
# Checks (used by validate-arrangement)
# ---------------------------------------------------------------------------


def check_techniques(arr: Arrangement) -> list[dict]:
    mlen = measure_quarters(arr.time_signature)
    issues = []
    by_string: dict[int, list[ArrNote]] = defaultdict(list)
    for n in arr.notes:
        if n.string is not None:
            by_string[n.string].append(n)
    for lst in by_string.values():
        lst.sort(key=lambda x: abs_beat(x, mlen))

    def add(sev, n, code, msg, fix=None):
        d = {"severity": sev, "measure": n.measure, "beat": round(n.beat, 3), "code": code, "message": msg}
        if fix:
            d["suggestion"] = fix
        issues.append(d)

    for n in arr.notes:
        toks = tokens(n)
        if not toks:
            continue
        unknown = [t for t in toks if t not in TOKENS]
        if unknown:
            add("warning", n, "unknown_technique", f"unknown technique(s) {unknown}",
                f"use one of: {', '.join(sorted(TOKENS))}")
        if n.fret == 0 and any(t in PITCH_EFFECTS for t in toks):
            add("warning", n, "open_string_effect", "vibrato/bend is impossible on an open string",
                "move the note to a fretted position (lock it) or drop the effect")
        if any(t in PITCH_EFFECTS for t in toks) and n.role in ("bass", "harmony") and arr.difficulty == "beginner":
            add("info", n, "effect_on_accompaniment", "pitch effect on an accompaniment note")
        if "bend" in toks or "bend_release" in toks:
            if n.string is not None and n.string >= 5:
                add("warning", n, "bend_low_string", "whole-tone bends on wound low strings are hard",
                    "use bend_half or vibrato")
        legato = [t for t in toks if t in LEGATO]
        if legato and n.string is not None:
            lst = by_string[n.string]
            i = lst.index(n)
            prev = lst[i - 1] if i > 0 else None
            if prev is None:
                add("warning", n, "legato_without_source", f"'{legato[0]}' needs a previous note on string {n.string}")
                continue
            gap = abs_beat(n, mlen) - (abs_beat(prev, mlen) + prev.duration)
            if gap > 0.26:
                add("warning", n, "legato_gap", f"'{legato[0]}' starts {gap:.2f} beats after the previous note ends",
                    "lengthen the previous note or remove the legato mark")
            p0, p1 = name_to_midi(prev.pitch), name_to_midi(n.pitch)
            if legato[0] == "h" and p1 <= p0:
                add("warning", n, "hammer_direction", "hammer-on must go up in pitch", "use 'p' (pull-off)")
            if legato[0] == "p" and p1 >= p0:
                add("warning", n, "pull_direction", "pull-off must go down in pitch", "use 'h' (hammer-on)")
            if legato[0] in ("h", "p") and abs(p1 - p0) > 5:
                add("warning", n, "legato_stretch", f"{abs(p1 - p0)}-semitone hammer/pull is a big stretch",
                    "use a slide or pick the note")
            if legato[0] == "slide" and abs((n.fret or 0) - (prev.fret or 0)) > 9:
                add("info", n, "long_slide", "very long slide")
            others = [m for m in arr.notes if m is not n and abs(abs_beat(m, mlen) - abs_beat(n, mlen)) < 1e-6]
            if legato[0] in ("h", "p") and len(others) >= 2:
                add("info", n, "legato_in_chord", "hammer/pull while other notes are struck sounds weak")
    return issues


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


def _seconds_map(arr: Arrangement, score=None, bar_offset: int = 0):
    """Return f(note) -> seconds in the recording (score measure_starts if available)."""
    mlen = measure_quarters(arr.time_signature)
    spq = 60.0 / arr.tempo_bpm
    starts = list(score.measure_starts) if score is not None and score.measure_starts else None

    def f(n: ArrNote) -> float | None:
        if starts is None:
            return None
        m = n.measure + bar_offset
        if not 1 <= m <= len(starts):
            return None
        bar_len = (starts[m] - starts[m - 1]) if m < len(starts) else mlen * spq
        return starts[m - 1] + n.beat / mlen * bar_len
    return f


def hand_busy(n: ArrNote, notes: list[ArrNote], mlen: float, portion: float = 1.0) -> bool:
    """True if other fretted notes start while `n` rings (fretting hand can't do vibrato/bends)."""
    t0 = abs_beat(n, mlen)
    t1 = t0 + n.duration * portion
    for m in notes:
        if m is n or not m.fret or m.string == n.string:
            continue
        tm = abs_beat(m, mlen)
        if t0 + 1e-6 < tm < t1 - 1e-6:
            return True
    return False


def suggest(arr: Arrangement, expression: dict | None = None, score=None, bar_offset: int = 0,
            idiomatic: bool = True, density_bars: float = 2.0) -> list[dict]:
    mlen = measure_quarters(arr.time_signature)
    spq = 60.0 / arr.tempo_bpm
    diff = DIFFICULTIES[arr.difficulty]
    notes = sorted([n for n in arr.notes if n.string is not None], key=lambda n: (abs_beat(n, mlen), n.string))
    by_onset: dict[float, list[ArrNote]] = defaultdict(list)
    for n in notes:
        by_onset[round(abs_beat(n, mlen), 4)].append(n)
    lead_roles = ("melody", "counter")
    sugg: list[dict] = []

    def propose(n, tok, kind, conf, reason):
        if tok in tokens(n):
            return
        sugg.append({"measure": n.measure, "beat": n.beat, "pitch": midi_to_name(name_to_midi(n.pitch)),
                     "string": n.string, "fret": n.fret, "role": n.role, "technique": tok,
                     "kind": kind, "confidence": round(conf, 2), "reason": reason, "_note": n})

    # 1) Evidence from the recording
    if expression and score is not None:
        to_sec = _seconds_map(arr, score, bar_offset)
        ev = expression.get("notes", [])
        for n in notes:
            t = to_sec(n)
            if t is None:
                continue
            p = name_to_midi(n.pitch)
            match = None
            for e in ev:
                if abs(e["start"] - t) <= 0.09 and (e["pitch"] - p) % 12 == 0:
                    match = e
                    break
            if not match:
                continue
            if n.role not in lead_roles:
                continue  # accompaniment inherits chord artefacts; keep it plain
            struck = by_onset[round(abs_beat(n, mlen), 4)]
            if len(struck) >= 3:
                continue
            busy = hand_busy(n, notes, mlen, 0.75)
            if "vibrato" in match and n.fret:
                v = match["vibrato"]
                tok = "wide_vibrato" if v["depth_cents"] >= 45 else "vibrato"
                conf = 0.5 + 0.5 * v.get("confidence", 0.5)
                why = f"recording has {v['rate_hz']} Hz vibrato, ±{v['depth_cents']} cents"
                if busy:
                    conf, why = 0.4, why + "; fretted accompaniment moves under it — use open-string " \
                        "bass or sustain the bass to make the vibrato playable"
                propose(n, tok, "evidence", conf, why)
            if "bend" in match and n.fret and n.string is not None and n.string <= 4 and not busy:
                b = match["bend"]
                tok = "bend_release" if b.get("release") else ("bend_half" if b["cents"] < 150 else "bend")
                propose(n, tok, "evidence", 0.8, f"recording bends this note by {b['cents']} cents")

    if not idiomatic:
        return _finish(sugg)

    # 2) Idiomatic: vibrato on long, exposed, fretted lead notes (phrase ends first)
    candidates = []
    for n in notes:
        if n.role not in lead_roles or not n.fret:
            continue
        sec = n.duration * spq
        if n.duration < 1.5 or sec < 0.6:
            continue
        struck = by_onset[round(abs_beat(n, mlen), 4)]
        fretted_together = sum(1 for m in struck if m.fret)
        if fretted_together > 2:
            continue  # hand is busy holding a chord shape
        busy = hand_busy(n, notes, mlen, 0.75)
        end = abs_beat(n, mlen) + n.duration
        nxt = [m for m in notes if m.role in lead_roles and abs_beat(m, mlen) >= end - 1e-6]
        gap = (abs_beat(nxt[0], mlen) - end) if nxt else 4.0
        score_ = 0.35 + 0.1 * min(3.0, n.duration - 1.5) + (0.2 if gap >= 0.5 else 0) + \
            (0.1 if n.string and n.string <= 3 else 0)
        if arr.difficulty == "beginner":
            score_ -= 0.2
        why = f"held {n.duration:g} beats ({sec:.1f}s) on a fretted string" + \
              (", phrase end" if gap >= 0.5 else "")
        if busy:
            score_ = min(score_, 0.4)
            why += "; fretted accompaniment moves under it — playable only if simplified"
        candidates.append((score_, n, why))
    # density control: best candidates first, at most one per `density_bars` bars
    candidates.sort(key=lambda c: -c[0])
    taken: list[float] = []
    window = density_bars * mlen
    for sc, n, why in candidates:
        t = abs_beat(n, mlen)
        if any(abs(t - x) < window for x in taken):
            continue
        taken.append(t)
        propose(n, "vibrato", "idiomatic", min(0.85, sc), why)

    # 3) Legato and slides between consecutive lead notes on the same string
    lead = [n for n in notes if n.role in lead_roles]
    by_string: dict[int, list[ArrNote]] = defaultdict(list)
    for n in lead:
        by_string[n.string].append(n)
    for lst in by_string.values():
        lst.sort(key=lambda x: abs_beat(x, mlen))
        for a, b in zip(lst, lst[1:]):
            ta, tb = abs_beat(a, mlen), abs_beat(b, mlen)
            gap = tb - (ta + a.duration)
            if gap > 0.13 or tb - ta > 1.5:
                continue
            pa, pb = name_to_midi(a.pitch), name_to_midi(b.pitch)
            df = (b.fret or 0) - (a.fret or 0)
            struck_b = by_onset[round(tb, 4)]
            alone = len(struck_b) <= 2  # at most a bass note under it
            if pa == pb:
                continue
            if 1 <= abs(df) <= 3 and abs(pb - pa) <= 3 and alone and tb - ta <= 0.75:
                tok = "h" if pb > pa else "p"
                propose(b, tok, "idiomatic", 0.55 if diff.name != "beginner" else 0.35,
                        f"{'hammer-on' if tok == 'h' else 'pull-off'} {midi_to_name(pa)}→{midi_to_name(pb)} "
                        f"on string {b.string} smooths a quick step")
            elif 3 <= abs(df) <= 7 and b.duration >= 0.5 and alone and a.fret and b.fret:
                propose(b, "slide", "idiomatic", 0.6,
                        f"covers a {abs(df)}-fret position shift on string {b.string} audibly and smoothly")
    return _finish(sugg)


def _finish(sugg: list[dict]) -> list[dict]:
    # one suggestion per note: evidence beats idiomatic; legato + vibrato may combine
    out, seen = [], {}
    for s in sorted(sugg, key=lambda s: (s["kind"] != "evidence", -s["confidence"])):
        key = id(s["_note"])
        prev = seen.get(key)
        if prev is None:
            seen[key] = [s["technique"]]
            out.append(s)
        else:
            pitch_fx = [t for t in prev if t in PITCH_EFFECTS]
            if s["technique"] in PITCH_EFFECTS and pitch_fx:
                continue
            if s["technique"] in LEGATO and any(t in LEGATO for t in prev):
                continue
            prev.append(s["technique"])
            out.append(s)
    out.sort(key=lambda s: (s["measure"], s["beat"]))
    return out


def apply(sugg: list[dict], level: str = "safe", min_confidence: float = 0.5) -> int:
    """Apply suggestions in place. level: evidence | safe | all."""
    n_applied = 0
    for s in sugg:
        if level == "evidence" and s["kind"] != "evidence":
            continue
        if level == "safe" and s["confidence"] < min_confidence:
            continue
        note: ArrNote = s["_note"]
        if s["technique"] in PITCH_EFFECTS and any(t in PITCH_EFFECTS for t in tokens(note)):
            continue
        add_token(note, s["technique"])
        s["applied"] = True
        n_applied += 1
    return n_applied


def public(sugg: list[dict]) -> list[dict]:
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in sugg]
