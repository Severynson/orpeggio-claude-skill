"""Physical playability checks for a fretted arrangement."""

from __future__ import annotations

from collections import defaultdict

from arpeggio.arrangement.instruments import DIFFICULTIES, Fretboard
from arpeggio.arrangement.tablature import abs_beat, measure_quarters
from arpeggio.schema import Arrangement
from arpeggio.theory import midi_to_name, name_to_midi


def validate(arr: Arrangement) -> dict:
    fb = Fretboard.from_spec(arr.tuning, arr.capo, arr.instrument)
    diff = DIFFICULTIES[arr.difficulty]
    mlen = measure_quarters(arr.time_signature)
    spq = 60.0 / arr.tempo_bpm
    issues: list[dict] = []

    def add(sev, n_or_pos, code, msg, fix=None):
        if hasattr(n_or_pos, "measure"):
            m, b = n_or_pos.measure, n_or_pos.beat
        else:
            m, b = n_or_pos
        d = {"severity": sev, "measure": m, "beat": round(b, 3), "code": code, "message": msg}
        if fix:
            d["suggestion"] = fix
        issues.append(d)

    notes = sorted(arr.notes, key=lambda n: (abs_beat(n, mlen), -name_to_midi(n.pitch)))
    for n in notes:
        p = name_to_midi(n.pitch)
        if n.beat < 0 or n.beat >= mlen - 1e-6:
            add("error", n, "beat_out_of_measure", f"beat {n.beat} outside a {mlen}-quarter measure")
        if n.duration <= 0:
            add("error", n, "bad_duration", "duration must be > 0")
        if p < fb.lowest or p > fb.highest:
            add("error", n, "out_of_range", f"{midi_to_name(p)} outside instrument range "
                f"{midi_to_name(fb.lowest)}–{midi_to_name(fb.highest)}", "transpose by an octave")
        if n.string is None or n.fret is None:
            add("error", n, "unassigned", f"{midi_to_name(p)} has no string/fret",
                "run optimize-tablature")
            continue
        if not 1 <= n.string <= fb.n_strings:
            add("error", n, "bad_string", f"string {n.string} does not exist")
            continue
        if fb.pitch_at(n.string, n.fret) != p:
            add("error", n, "pitch_mismatch",
                f"string {n.string} fret {n.fret} sounds {midi_to_name(fb.pitch_at(n.string, n.fret))}, "
                f"not {midi_to_name(p)}", "re-run optimize-tablature or fix the position")
        if n.fret < 0 or n.fret > fb.frets - arr.capo:
            add("error", n, "fret_out_of_range", f"fret {n.fret} not on the neck")
        elif n.fret > diff.max_fret:
            add("warning", n, "high_fret", f"fret {n.fret} above the {arr.difficulty} limit ({diff.max_fret})")

    # Onset groups
    onsets: dict[float, list] = defaultdict(list)
    for n in notes:
        onsets[round(abs_beat(n, mlen), 4)].append(n)
    times = sorted(onsets)
    prev_hand = None
    prev_t = None
    for t in times:
        group = onsets[t]
        pos = (group[0].measure, group[0].beat)
        if len(group) > fb.n_strings:
            add("error", pos, "too_many_notes", f"{len(group)} notes struck at once on {fb.n_strings} strings")
        elif len(group) > 4 and arr.difficulty != "advanced":
            add("warning", pos, "right_hand", f"{len(group)} simultaneous notes exceed p-i-m-a; needs a strum")
        elif len(group) > diff.max_simultaneous:
            add("warning", pos, "dense_chord", f"{len(group)} simultaneous notes for {arr.difficulty}")
        strings = [n.string for n in group if n.string]
        dup = {s for s in strings if strings.count(s) > 1}
        if dup:
            add("error", pos, "string_collision", f"several notes on string(s) {sorted(dup)} at once",
                "move one note to another string or drop it")
        # sounding set = struck now + still ringing
        sounding = [n for n in notes if n.string and abs_beat(n, mlen) <= t + 1e-6 < abs_beat(n, mlen) + n.duration - 1e-6]
        struck_strings = {n.string for n in group}
        for n in sounding:
            if n in group:
                continue
            if n.string in struck_strings:
                remain = abs_beat(n, mlen) + n.duration - t
                if remain > 0.5 and n.role in ("melody", "bass"):
                    add("warning", pos, "cut_sustain",
                        f"{n.role} {midi_to_name(name_to_midi(n.pitch))} (m{n.measure} b{n.beat}) on string "
                        f"{n.string} is cut {remain:.2f} beats early", "move one of the notes to another string "
                        "or shorten the sustained note")
        held = [n for n in sounding if n.fret and n.fret > 0 and n.string]
        frets = [n.fret for n in held]
        if frets:
            span = max(frets) - min(frets)
            if span > diff.max_span + 2:
                add("error", pos, "stretch", f"fret span {span} ({min(frets)}–{max(frets)}) while notes ring",
                    "shorten held notes, use open strings or change position")
            elif span > diff.max_span:
                add("warning", pos, "stretch", f"fret span {span} above {arr.difficulty} limit {diff.max_span}")
            positions = {(n.string, n.fret) for n in held}
            lowest = min(frets)
            at_low = sum(1 for s, f in positions if f == lowest)
            fingers = (1 if at_low > 1 else at_low) + sum(1 for s, f in positions if f != lowest)
            if fingers > 4:
                add("error", pos, "fingers", f"{fingers} fretting fingers needed (max 4)",
                    "drop or re-voice a harmony note")
            hand = sum(frets) / len(frets)
            if prev_hand is not None and prev_t is not None:
                dt = (t - prev_t) * spq
                move = abs(hand - prev_hand)
                if dt > 0 and move / dt > diff.shift_speed and move >= 4:
                    add("warning", pos, "fast_shift", f"hand moves {move:.0f} frets in {dt:.2f}s",
                        "choose positions closer together or use an open string to cover the shift")
            prev_hand, prev_t = hand, t
        # melody should be the top voice
        mel = [n for n in group if n.role == "melody"]
        if mel:
            top = max(name_to_midi(n.pitch) for n in mel)
            above = [n for n in sounding if n.role not in ("melody",) and name_to_midi(n.pitch) > top]
            if above:
                add("info", pos, "melody_not_top", "accompaniment sounds above the melody",
                    "lower the accompaniment an octave")

    # Measure-level checks
    if arr.measure_count:
        used = {n.measure for n in notes}
        empty = [m for m in range(1, arr.measure_count + 1) if m not in used]
        if empty:
            add("info", (empty[0], 0), "empty_measures", f"{len(empty)} empty measures: {_ranges(empty)}")

    from arpeggio.arrangement.techniques import check_techniques

    issues.extend(check_techniques(arr))
    counts = defaultdict(int)
    for i in issues:
        counts[i["severity"]] += 1
    return {"valid": counts["error"] == 0, "errors": counts["error"], "warnings": counts["warning"],
            "infos": counts["info"], "notes": len(notes), "difficulty": arr.difficulty,
            "issues": issues}


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for x in nums:
        if start is None:
            start = prev = x
        elif x == prev + 1:
            prev = x
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = x
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)
