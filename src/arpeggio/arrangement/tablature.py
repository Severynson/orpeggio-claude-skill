"""Fretboard optimisation: assign (string, fret) to every arrangement note.

Dynamic programming (Viterbi) over onset groups. Each group's candidate
fingerings must put simultaneous notes on distinct strings within the fret
span allowed by the difficulty. The path cost balances:

* local cost — fret height, span, open-string preference, finger count;
* transition cost — hand-position movement relative to available time,
  cutting off notes that should still ring on the same string, and keeping
  ringing fretted notes inside one hand span.

Unplayable groups are repaired (unless disabled) by octave-shifting, then
dropping, the least important notes (harmony first, never the melody), and
every change is reported.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from arpeggio.arrangement.instruments import DIFFICULTIES, Difficulty, Fretboard
from arpeggio.schema import ArrNote, Arrangement
from arpeggio.theory import midi_to_name, name_to_midi

ROLE_PRIORITY = {"melody": 0, "bass": 1, "counter": 2, "harmony": 3, "percussive": 4}


def measure_quarters(time_signature: str) -> float:
    num, _, den = time_signature.partition("/")
    return int(num) * 4.0 / int(den or 4)


def abs_beat(n: ArrNote, mlen: float) -> float:
    return (n.measure - 1) * mlen + n.beat


@dataclass
class Candidate:
    positions: tuple[tuple[int, int], ...]  # per note in group order
    cost: float
    hand: float | None  # mean fretted fret, None if all open


def _local_cost(pos, roles, diff: Difficulty) -> tuple[float, float | None] | None:
    strings = [s for s, _ in pos]
    if len(set(strings)) != len(strings):
        return None
    fretted = [f for _, f in pos if f > 0]
    span = (max(fretted) - min(fretted)) if fretted else 0
    if span > diff.max_span:
        return None
    if fretted and max(fretted) > diff.max_fret + 2:
        return None
    # finger count: a barre at the lowest fret counts once
    if fretted:
        lowest = min(fretted)
        fingers = (1 if fretted.count(lowest) > 1 else fretted.count(lowest)) + \
            sum(1 for f in fretted if f != lowest)
        if fingers > diff.max_fingers:
            return None
    cost = 0.0
    for (s, f), role in zip(pos, roles):
        if f == 0:
            cost -= 0.25
        else:
            cost += 0.06 * f
        if f > diff.max_fret:
            cost += 1.5
        if role == "bass" and s <= 2:
            cost += 0.6  # bass on treble strings sounds thin
        if role == "melody" and s >= 5:
            cost += 0.3
    cost += 0.35 * span * span / max(1, diff.max_span)
    hand = sum(fretted) / len(fretted) if fretted else None
    return cost, hand


def _candidates(pitches, roles, locked, fb: Fretboard, diff: Difficulty, limit: int = 80):
    options = []
    for p, lk in zip(pitches, locked):
        if lk is not None:
            options.append([lk])
        else:
            opts = fb.positions(p, max_fret=diff.max_fret + 2)
            if not opts:
                return []
            options.append(opts)
    cands = []
    for combo in itertools.product(*options):
        r = _local_cost(combo, roles, diff)
        if r is None:
            continue
        cands.append(Candidate(combo, r[0], r[1]))
    cands.sort(key=lambda c: c.cost)
    return cands[:limit]


def optimize(arr: Arrangement, tuning=None, capo: int | None = None,
             difficulty: str | None = None, repair: bool = True) -> tuple[Arrangement, dict]:
    arr = arr.model_copy(deep=True)
    if tuning is not None:
        arr.tuning = tuning
    if capo is not None:
        arr.capo = capo
    if difficulty is not None:
        arr.difficulty = difficulty
    fb = Fretboard.from_spec(arr.tuning, arr.capo, arr.instrument)
    diff = DIFFICULTIES[arr.difficulty]
    mlen = measure_quarters(arr.time_signature)
    sec_per_q = 60.0 / arr.tempo_bpm
    changes: list[dict] = []

    # Range repair: fold out-of-range pitches by octaves
    for n in arr.notes:
        p = name_to_midi(n.pitch)
        orig = p
        while p < fb.lowest:
            p += 12
        top = fb.open_midi_low_to_high[-1] + diff.max_fret + 2
        while p > top and p - 12 >= fb.lowest:
            p -= 12
        if p != orig:
            if not repair:
                continue
            changes.append({"measure": n.measure, "beat": n.beat, "action": "octave_shift",
                            "from": midi_to_name(orig), "to": midi_to_name(p), "role": n.role})
        n.pitch = p
        # A locked position must actually produce the pitch; otherwise unlock it.
        if n.locked and n.string is not None and n.fret is not None:
            if not 1 <= n.string <= fb.n_strings or fb.pitch_at(n.string, n.fret) != p:
                changes.append({"measure": n.measure, "beat": n.beat, "action": "unlock",
                                "reason": f"string {n.string} fret {n.fret} does not sound {midi_to_name(p)}"})
                n.locked = False
                n.string = n.fret = None

    # Group by onset
    groups: dict[float, list[ArrNote]] = {}
    for n in arr.notes:
        groups.setdefault(round(abs_beat(n, mlen), 4), []).append(n)
    onsets = sorted(groups)

    layers = []  # per onset: (notes, candidates)
    for t in onsets:
        notes = sorted(groups[t], key=lambda n: -name_to_midi(n.pitch))
        # de-duplicate identical pitches struck together
        uniq, seen = [], set()
        for n in notes:
            p = name_to_midi(n.pitch)
            if p in seen:
                changes.append({"measure": n.measure, "beat": n.beat, "action": "drop_duplicate",
                                "note": midi_to_name(p)})
                arr.notes.remove(n)
                continue
            seen.add(p)
            uniq.append(n)
        notes = uniq
        cands = _try(notes, fb, diff)
        while not cands and repair and len(notes) > 1:
            victim = max(notes, key=lambda n: (ROLE_PRIORITY.get(n.role, 5), -abs(name_to_midi(n.pitch) - 60)))
            if victim.role == "melody" and all(x.role == "melody" for x in notes):
                victim = notes[-1]
            # first try moving the victim an octave toward the hand position
            moved = False
            for shift in (12, -12):
                p = name_to_midi(victim.pitch) + shift
                if victim.role in ("harmony", "counter") and fb.lowest <= p <= fb.highest:
                    old = victim.pitch
                    victim.pitch = p
                    if _try(notes, fb, diff):
                        changes.append({"measure": victim.measure, "beat": victim.beat,
                                        "action": "octave_shift", "from": midi_to_name(name_to_midi(old)),
                                        "to": midi_to_name(p), "role": victim.role})
                        moved = True
                        break
                    victim.pitch = old
            if not moved:
                notes.remove(victim)
                arr.notes.remove(victim)
                changes.append({"measure": victim.measure, "beat": victim.beat, "action": "drop",
                                "note": midi_to_name(name_to_midi(victim.pitch)), "role": victim.role,
                                "reason": "no playable fingering for this chord"})
            cands = _try(notes, fb, diff)
        layers.append((t, notes, cands))

    # Viterbi (unplayable layers split the chain into independent segments)
    INF = float("inf")
    layer_costs: list[list[float]] = []
    back: list[list[int]] = []
    unplayable = []
    for li, (t, notes, cands) in enumerate(layers):
        if not cands:
            if notes:
                unplayable.append({"measure": notes[0].measure, "beat": notes[0].beat,
                                   "notes": [midi_to_name(name_to_midi(n.pitch)) for n in notes]})
            layer_costs.append([])
            back.append([])
            continue
        cur = [c.cost for c in cands]
        bp = [-1] * len(cands)
        if li > 0 and layers[li - 1][2]:
            pt, pnotes, pcands = layers[li - 1]
            prev = layer_costs[li - 1]
            dt = max(1e-3, (t - pt) * sec_per_q)
            for j, c in enumerate(cands):
                best, arg = INF, 0
                for i, pc in enumerate(pcands):
                    v = prev[i] + _transition(pnotes, pc, pt, notes, c, t, mlen, dt, diff)
                    if v < best:
                        best, arg = v, i
                cur[j] += best
                bp[j] = arg
        layer_costs.append(cur)
        back.append(bp)

    chosen: list[Candidate | None] = [None] * len(layers)
    j = None
    for k in range(len(layers) - 1, -1, -1):
        cands = layers[k][2]
        if not cands:
            j = None
            continue
        if j is None:  # end of a segment: pick its cheapest final state
            j = min(range(len(cands)), key=lambda q: layer_costs[k][q])
        chosen[k] = cands[j]
        j = back[k][j] if back[k][j] >= 0 else None

    _refine(layers, chosen, mlen, sec_per_q, diff)

    assigned = 0
    for (t, notes, cands), c in zip(layers, chosen):
        if c is None:
            continue
        for n, (s, f) in zip(notes, c.positions):
            n.string, n.fret = s, f
            assigned += 1

    if repair:
        _repair_sustains(arr, fb, diff, mlen, changes)
    arr.notes.sort(key=lambda n: (n.measure, n.beat, -name_to_midi(n.pitch)))
    frets = [n.fret for n in arr.notes if n.fret]
    report = {
        "assigned": assigned, "total_notes": len(arr.notes),
        "unplayable_groups": unplayable, "changes": changes,
        "fret_range": [min(frets), max(frets)] if frets else [0, 0],
        "open_string_ratio": round(sum(1 for n in arr.notes if n.fret == 0) / max(1, len(arr.notes)), 3),
        "fretboard": fb.describe(), "difficulty": arr.difficulty,
    }
    return arr, report


def _try(notes, fb, diff):
    pitches = [name_to_midi(n.pitch) for n in notes]
    roles = [n.role for n in notes]
    locked = [(n.string, n.fret) if n.locked and n.string and n.fret is not None else None for n in notes]
    return _candidates(pitches, roles, locked, fb, diff)


def _move_cost(h0, h1, dt, diff: Difficulty) -> float:
    if h0 is None or h1 is None:
        return 0.0
    move = abs(h1 - h0)
    cost = 0.25 * move
    speed = move / max(dt, 1e-3)
    if speed > diff.shift_speed:
        cost += 0.5 * (speed / diff.shift_speed)
    return cost


def _ring_cost(ringing, c: Candidate, t, mlen, diff: Difficulty) -> float:
    """ringing: [((string, fret), note)] still sounding at time t."""
    if not ringing:
        return 0.0
    cost = 0.0
    cur_strings = {s for s, _ in c.positions}
    for (s, f), n in ringing:
        if s in cur_strings:
            remain = abs_beat(n, mlen) + n.duration - t
            cost += (1.2 if n.role in ("melody", "bass") else 0.6) * min(2.0, remain)
    held = [f for (s, f), _ in ringing if f > 0 and s not in cur_strings] + [f for _, f in c.positions if f > 0]
    if held and max(held) - min(held) > diff.max_span:
        cost += 1.5 + 0.5 * (max(held) - min(held) - diff.max_span)
    return cost


def _transition(pnotes, pc: Candidate, pt, notes, c: Candidate, t, mlen, dt, diff: Difficulty) -> float:
    ringing = [(pos, n) for pos, n in zip(pc.positions, pnotes) if abs_beat(n, mlen) + n.duration > t + 1e-3]
    return _move_cost(pc.hand, c.hand, dt, diff) + _ring_cost(ringing, c, t, mlen, diff)


def _refine(layers, chosen, mlen, sec_per_q, diff: Difficulty, sweeps: int = 2) -> None:
    """Iterated conditional modes with the *full* set of ringing notes.

    Viterbi only sees the previous onset; a bass note held for two beats can
    still clash with a chord three onsets later. Re-pick each group's fingering
    given every note still ringing and the neighbours' current choices.
    """
    idx = [k for k, c in enumerate(chosen) if c is not None]
    for _ in range(sweeps):
        changed = False
        for pos_i, k in enumerate(idx):
            t, notes, cands = layers[k]
            if len(cands) < 2:
                continue
            ringing = []
            for kk in reversed(idx[max(0, pos_i - 24):pos_i]):
                tt, nn, _ = layers[kk]
                if t - tt > 8:
                    break
                for p_, n_ in zip(chosen[kk].positions, nn):
                    if abs_beat(n_, mlen) + n_.duration > t + 1e-3:
                        ringing.append((p_, n_))
            prev_k = idx[pos_i - 1] if pos_i > 0 else None
            next_k = idx[pos_i + 1] if pos_i + 1 < len(idx) else None
            prev_hand = chosen[prev_k].hand if prev_k is not None else None
            dt_prev = (t - layers[prev_k][0]) * sec_per_q if prev_k is not None else 1.0

            def total(c: Candidate) -> float:
                v = c.cost + _move_cost(prev_hand, c.hand, dt_prev, diff) + _ring_cost(ringing, c, t, mlen, diff)
                if next_k is not None:
                    nt, nnotes, _ = layers[next_k]
                    v += _transition(notes, c, t, nnotes, chosen[next_k], nt, mlen,
                                     (nt - t) * sec_per_q, diff)
                return v

            best = min(cands, key=total)
            if best is not chosen[k] and total(best) < total(chosen[k]) - 1e-9:
                chosen[k] = best
                changed = True
        if not changed:
            break


def _repair_sustains(arr: Arrangement, fb: Fretboard, diff: Difficulty, mlen: float, changes: list) -> None:
    """Shorten notes that physically cannot keep ringing (string reused, stretch)."""
    notes = sorted([n for n in arr.notes if n.string is not None], key=lambda n: abs_beat(n, mlen))
    onsets = sorted({round(abs_beat(n, mlen), 4) for n in notes})
    by_onset: dict[float, list[ArrNote]] = {}
    for n in notes:
        by_onset.setdefault(round(abs_beat(n, mlen), 4), []).append(n)
    active: list[ArrNote] = []
    for t in onsets:
        group = by_onset[t]
        active = [n for n in active if abs_beat(n, mlen) + n.duration > t + 1e-3]
        strings = {n.string for n in group}
        for n in list(active):
            if n.string in strings:
                old = n.duration
                n.duration = round(t - abs_beat(n, mlen), 4)
                if old - n.duration > 0.5 and n.role in ("melody", "bass"):
                    changes.append({"measure": n.measure, "beat": n.beat, "action": "shorten",
                                    "role": n.role, "from": old, "to": n.duration,
                                    "reason": "string re-used before the note ends"})
                active.remove(n)
        # stretch: release ringing accompaniment that is out of the hand span
        fretted_now = [n.fret for n in group if n.fret]
        if fretted_now:
            for n in sorted(list(active), key=lambda x: ROLE_PRIORITY.get(x.role, 5), reverse=True):
                held = fretted_now + [x.fret for x in active if x.fret]
                if max(held) - min(held) <= diff.max_span:
                    break
                if n.fret and n.role != "melody":
                    n.duration = round(t - abs_beat(n, mlen), 4)
                    active.remove(n)
        active.extend(group)


def chord_voicings(symbol: str, fb: Fretboard, difficulty: str = "intermediate",
                   max_results: int = 5) -> list[dict]:
    """Enumerate playable shapes for a chord symbol on this fretboard."""
    from arpeggio.theory import parse_chord

    c = parse_chord(symbol)
    pcs = set(c["pitch_classes"])
    bass_pc = c.get("bass_pc", c["root_pc"])
    diff = DIFFICULTIES[difficulty]
    n = fb.n_strings
    per_string = []
    for s in range(1, n + 1):
        opts = [None]
        for f in range(0, diff.max_fret + 1):
            if fb.pitch_at(s, f) % 12 in pcs:
                opts.append(f)
        per_string.append(opts)
    results = []
    for combo in itertools.product(*per_string):
        played = [(s + 1, f) for s, f in enumerate(combo) if f is not None]
        if len(played) < min(3, len(pcs)):
            continue
        # no muted strings between played ones (strummable), at most 2 muted bass strings
        idxs = [s for s, _ in played]
        if max(idxs) - min(idxs) + 1 != len(played):
            continue
        fretted = [f for _, f in played if f > 0]
        span = max(fretted) - min(fretted) if fretted else 0
        if span > min(diff.max_span, 4):
            continue
        lowest_string = max(idxs)
        if fb.pitch_at(lowest_string, combo[lowest_string - 1]) % 12 != bass_pc:
            continue
        sounding = {fb.pitch_at(s, f) % 12 for s, f in played}
        if not pcs.issubset(sounding) and not (len(pcs) >= 4 and len(pcs - sounding) == 1 and
                                                (c["root_pc"] + 7) % 12 in pcs - sounding):
            continue
        if fretted:
            lowest = min(fretted)
            fingers = (1 if fretted.count(lowest) > 1 else fretted.count(lowest)) + \
                sum(1 for f in fretted if f != lowest)
            if fingers > 4:
                continue
        pos = min(fretted) if fretted else 0
        score = 0.35 * pos + 0.4 * span + 0.15 * len(fretted) - 0.12 * (len(played) - len(fretted)) \
            - 0.1 * len(played)
        if fretted and fretted.count(min(fretted)) > 1 and len(fretted) > 3:
            score += 0.6  # barre
        shape = "".join("x" if f is None else (str(f) if f < 10 else f"({f})")
                        for f in reversed(combo))  # low E -> high e
        results.append({"chord": symbol, "shape": shape, "position": pos,
                        "strings": [{"string": s, "fret": f, "note": midi_to_name(fb.pitch_at(s, f))}
                                    for s, f in played],
                        "score": round(score, 3)})
    results.sort(key=lambda r: r["score"])
    seen, out = set(), []
    for r in results:
        if r["shape"] in seen:
            continue
        seen.add(r["shape"])
        out.append(r)
        if len(out) >= max_results:
            break
    return out


def sounding_at(notes: list[ArrNote], t: float, mlen: float) -> list[ArrNote]:
    return [n for n in notes if abs_beat(n, mlen) <= t + 1e-6 < abs_beat(n, mlen) + n.duration]


__all__ = ["optimize", "chord_voicings", "measure_quarters", "abs_beat", "sounding_at", "math"]
