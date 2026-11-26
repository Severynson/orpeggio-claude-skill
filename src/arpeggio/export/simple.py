"""MIDI, ASCII tablature and PDF export."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from arpeggio.arrangement.instruments import MIDI_PROGRAMS, Fretboard
from arpeggio.arrangement.tablature import abs_beat, measure_quarters
from arpeggio.schema import Arrangement
from arpeggio.theory import midi_to_name, name_to_midi


def export_midi(arr: Arrangement, path: str | Path, split_roles: bool = False,
                render_effects: bool = True) -> Path:
    """Write MIDI. Vibrato, bends and slides are rendered with pitch-bend messages
    on separate channels (pitch bend is per-channel), so they are audible."""
    import math

    import pretty_midi

    from arpeggio.arrangement.techniques import PITCH_EFFECTS, previous_on_string, tokens

    mlen = measure_quarters(arr.time_signature)
    spq = 60.0 / arr.tempo_bpm
    pm = pretty_midi.PrettyMIDI(initial_tempo=arr.tempo_bpm)
    num, _, den = arr.time_signature.partition("/")
    pm.time_signature_changes.append(pretty_midi.TimeSignature(int(num), int(den or 4), 0))
    program = MIDI_PROGRAMS.get(arr.instrument, 25)
    insts = {}
    fx_channels: list = []  # instruments used for effect notes, reused when free
    BEND_RANGE = 2.0  # semitones (GM default)

    def bend_value(semitones: float) -> int:
        return int(max(-8192, min(8191, round(semitones / BEND_RANGE * 8191))))

    for n in sorted(arr.notes, key=lambda x: abs_beat(x, mlen)):
        start = abs_beat(n, mlen) * spq
        end = start + n.duration * spq
        vel = 92 if n.role == "melody" else 72 if n.role == "bass" else 60
        pitch = name_to_midi(n.pitch)
        toks = tokens(n) if render_effects else []
        fx = [t for t in toks if t in PITCH_EFFECTS or t == "slide"]
        if "ghost" in toks:
            vel = 35
        if "accent" in toks:
            vel = min(127, vel + 20)
        if "staccato" in toks or "palm_mute" in toks:
            end = start + min(n.duration * spq, 0.12 if "palm_mute" in toks else 0.5 * n.duration * spq)
        if not fx:
            key = n.role if split_roles else "all"
            if key not in insts:
                insts[key] = pretty_midi.Instrument(program=program, name=f"{arr.instrument} {key}".strip())
            insts[key].notes.append(pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=end))
            continue
        inst = next((i for i in fx_channels if i._busy_until <= start), None)
        if inst is None:
            inst = pretty_midi.Instrument(program=program, name=f"{arr.instrument} fx{len(fx_channels) + 1}")
            inst._busy_until = 0.0
            fx_channels.append(inst)
        inst._busy_until = end + 0.05
        bends = []
        if "slide" in fx:  # glide in from the previous note on the string
            prev = previous_on_string(arr.notes, n, mlen)
            if prev is not None:
                d = name_to_midi(prev.pitch) - pitch
                d = max(-BEND_RANGE, min(BEND_RANGE, d))
                glide = min(0.09, 0.4 * (end - start))
                for k in range(7):
                    bends.append((start + glide * k / 6, d * (1 - k / 6)))
        if any(t in fx for t in ("bend", "bend_half", "bend_release")):
            amt = 1.0 if "bend_half" in fx else 2.0
            rise = min(0.15, 0.3 * (end - start))
            for k in range(9):
                bends.append((start + rise * k / 8, amt * k / 8))
            if "bend_release" in fx:
                fall_t = start + 0.6 * (end - start)
                for k in range(9):
                    bends.append((fall_t + rise * k / 8, amt * (1 - k / 8)))
        if "vibrato" in fx or "wide_vibrato" in fx:
            depth = 0.45 if "wide_vibrato" in fx else 0.22  # semitones, peak
            rate = 5.5
            v0 = start + min(0.25, 0.3 * (end - start))  # players start vibrato after the attack
            t = v0
            base = bends[-1][1] if bends else 0.0
            while t < end:
                ramp = min(1.0, (t - v0) / 0.2)
                bends.append((t, base + depth * ramp * math.sin(2 * math.pi * rate * (t - v0))))
                t += 0.02
        bends.append((end, 0.0))
        inst.notes.append(pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=end))
        for tb, semi in sorted(bends):
            inst.pitch_bends.append(pretty_midi.PitchBend(bend_value(semi), tb))
    pm.instruments.extend(insts.values())
    pm.instruments.extend(fx_channels)
    if len(pm.instruments) > 15:
        raise ValueError("too many simultaneous effect notes for MIDI channels")
    pm.write(str(path))
    return Path(path)


def tab_mark(n) -> str:
    """Fret number decorated with technique symbols (common tab conventions)."""
    from arpeggio.arrangement.techniques import tokens

    toks = tokens(n)
    mark = str(n.fret)
    if "harmonic" in toks:
        mark = f"<{n.fret}>"
    if "h" in toks:
        mark = "h" + mark
    elif "p" in toks:
        mark = "p" + mark
    elif "slide" in toks:
        mark = "/" + mark
    if "slide_in" in toks:
        mark = "/" + mark
    if "bend" in toks or "bend_half" in toks:
        mark += "b"
    if "bend_release" in toks:
        mark += "br"
    if "vibrato" in toks or "wide_vibrato" in toks:
        mark += "~"
    if "slide_out" in toks:
        mark += "\\"
    if "palm_mute" in toks:
        mark = "PM" + mark
    return mark


def render_ascii(arr: Arrangement, measures_per_line: int = 4, steps_per_quarter: int = 4,
                 measures: tuple[int, int] | None = None) -> str:
    fb = Fretboard.from_spec(arr.tuning, arr.capo, arr.instrument)
    mlen = measure_quarters(arr.time_signature)
    steps = int(round(mlen * steps_per_quarter))
    count = max([arr.measure_count or 0] + [n.measure for n in arr.notes] + [1])
    lo, hi = measures or (1, count)
    labels = []
    for s in range(1, fb.n_strings + 1):
        name = midi_to_name(fb.string_pitch(s) - arr.capo)[:-1]
        labels.append(name.lower() if s == 1 else name)
    width = max(len(x) for x in labels)
    labels = [x.ljust(width) for x in labels]

    def measure_block(m: int) -> tuple[list[str], str]:
        cols: list[dict[int, str]] = [dict() for _ in range(steps)]
        for n in arr.notes:
            if n.measure != m or n.string is None or n.fret is None:
                continue
            c = min(steps - 1, int(round(n.beat * steps_per_quarter)))
            mark = tab_mark(n)
            cols[c][n.string] = mark
        widths = [max([1] + [len(v) for v in col.values()]) + 1 for col in cols]
        rows = []
        for s in range(1, fb.n_strings + 1):
            row = "".join((col.get(s, "-")).ljust(w, "-") for col, w in zip(cols, widths))
            rows.append(row)
        chord_line = [" "] * sum(widths)
        pos = [sum(widths[:i]) for i in range(steps)]
        for c in arr.chords:
            if c.measure == m:
                i = min(steps - 1, int(round(c.beat * steps_per_quarter)))
                for k, ch in enumerate(c.chord):
                    if pos[i] + k < len(chord_line):
                        chord_line[pos[i] + k] = ch
        return rows, "".join(chord_line)

    header = [f"{arr.title}" + (f" — {arr.artist}" if arr.artist else ""),
              f"{arr.instrument.replace('_', ' ')} · tuning {' '.join(fb.tuning_names)}"
              + (f" · capo {arr.capo}" if arr.capo else "")
              + f" · {arr.time_signature} · ♩={round(arr.tempo_bpm)} · {arr.difficulty}", ""]
    out = header
    section_at = {s.start_measure: s.name for s in arr.sections}
    for start in range(lo, hi + 1, measures_per_line):
        block_ms = list(range(start, min(hi, start + measures_per_line - 1) + 1))
        blocks = [measure_block(m) for m in block_ms]
        names = [section_at[m] for m in block_ms if m in section_at]
        if names:
            out.append(f"[{names[0]}]")
        num_line = " " * (width + 1)
        chord_line = " " * (width + 1)
        for m, (rows, cl) in zip(block_ms, blocks):
            seg_w = len(rows[0]) + 1
            num_line += str(m).ljust(seg_w)
            chord_line += cl.ljust(seg_w - 1) + " "
        out.append(num_line.rstrip())
        if chord_line.strip():
            out.append(chord_line.rstrip())
        for si in range(fb.n_strings):
            out.append(labels[si] + "|" + "|".join(rows[si] for rows, _ in blocks) + "|")
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

MUSESCORE_CANDIDATES = [
    "mscore", "musescore", "mscore4", "musescore4", "MuseScore4", "mscore3",
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore Studio 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
]


def find_musescore() -> str | None:
    env = os.environ.get("ARPEGGIO_MUSESCORE") or os.environ.get("MUSCRIPTOR_MUSESCORE")
    for c in ([env] if env else []) + MUSESCORE_CANDIDATES:
        if not c:
            continue
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    return None


def export_pdf(arr: Arrangement, path: str | Path, musicxml_path: str | Path | None = None) -> dict:
    """Engrave with MuseScore if available, else typeset the ASCII tab as a PDF."""
    path = Path(path)
    ms = find_musescore()
    if ms:
        from arpeggio.export.musicxml import export_musicxml

        xml = Path(musicxml_path) if musicxml_path else path.with_suffix(".musicxml")
        if not xml.exists():
            export_musicxml(arr, xml)
        r = subprocess.run([ms, "-o", str(path), str(xml)], capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and path.exists():
            return {"path": str(path), "engine": "musescore", "musescore": ms}
        fallback_reason = f"MuseScore failed: {r.stderr[-300:]}"
    else:
        fallback_reason = "MuseScore not found (install MuseScore 4 or set ARPEGGIO_MUSESCORE)"
    text = render_ascii(arr, measures_per_line=3)
    write_text_pdf(text, path, title=arr.title)
    return {"path": str(path), "engine": "ascii-tab", "note": fallback_reason}


def write_text_pdf(text: str, path: str | Path, title: str = "", font_size: float = 8.0) -> Path:
    """Minimal dependency-free PDF writer: monospace text, A4 pages."""
    page_w, page_h, margin = 595.0, 842.0, 36.0
    longest = max((len(x) for x in text.expandtabs(4).splitlines()), default=1)
    font_size = min(font_size, (page_w - 2 * margin) / (0.6 * longest))  # Courier glyph = 0.6 em
    leading = font_size * 1.18
    lines_per_page = int((page_h - 2 * margin) / leading)
    lines = text.expandtabs(4).splitlines()
    # keep tab systems together: break pages on blank lines when close to the bottom
    pages, cur = [], []
    for ln in lines:
        if len(cur) >= lines_per_page:
            cut = max((i for i, x in enumerate(cur) if not x.strip()), default=len(cur) - 1)
            if cut < lines_per_page * 0.6:
                cut = len(cur) - 1
            pages.append(cur[:cut + 1])
            cur = cur[cut + 1:]
        cur.append(ln)
    if cur:
        pages.append(cur)

    def esc(s: str) -> str:
        s = s.replace("—", "-").replace("♩", "q").replace("·", "|")
        s = s.encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objs.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objs)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    pages_id_placeholder = len(objs) + 1
    add("PAGES")  # placeholder
    page_ids = []
    for pi, pl in enumerate(pages):
        body = [f"BT /F1 {font_size} Tf {leading:.2f} TL {margin} {page_h - margin} Td"]
        for ln in pl:
            body.append(f"({esc(ln)}) Tj T*")
        body.append("ET")
        body.append(f"BT /F1 7 Tf {page_w - margin - 60} {margin / 2} Td (page {pi + 1}/{len(pages)}) Tj ET")
        stream = "\n".join(body).encode("latin-1", "replace")
        cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        pid = add(f"<< /Type /Page /Parent {pages_id_placeholder} 0 R /MediaBox [0 0 {page_w} {page_h}] "
                  f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {cid} 0 R >>")
        page_ids.append(pid)
    objs[pages_id_placeholder - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] "
                                      f"/Count {len(page_ids)} >>").encode()
    info = add(f"<< /Title ({esc(title)}) /Producer (Arpeggio) >>")
    catalog = add(f"<< /Type /Catalog /Pages {pages_id_placeholder} 0 R >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    Path(path).write_bytes(bytes(out))
    return Path(path)
