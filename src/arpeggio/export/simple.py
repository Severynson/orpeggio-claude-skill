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


def export_midi(arr: Arrangement, path: str | Path, split_roles: bool = False) -> Path:
    import pretty_midi

    mlen = measure_quarters(arr.time_signature)
    spq = 60.0 / arr.tempo_bpm
    pm = pretty_midi.PrettyMIDI(initial_tempo=arr.tempo_bpm)
    num, _, den = arr.time_signature.partition("/")
    pm.time_signature_changes.append(pretty_midi.TimeSignature(int(num), int(den or 4), 0))
    program = MIDI_PROGRAMS.get(arr.instrument, 25)
    insts = {}
    for n in arr.notes:
        key = n.role if split_roles else "all"
        if key not in insts:
            insts[key] = pretty_midi.Instrument(program=program, name=f"{arr.instrument} {key}".strip())
        start = abs_beat(n, mlen) * spq
        vel = 92 if n.role == "melody" else 72 if n.role == "bass" else 60
        insts[key].notes.append(pretty_midi.Note(velocity=vel, pitch=name_to_midi(n.pitch),
                                                 start=start, end=start + n.duration * spq))
    pm.instruments.extend(insts.values())
    pm.write(str(path))
    return Path(path)


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
            mark = str(n.fret)
            tech = (n.technique or "").lower()
            if tech in ("harmonic", "nh"):
                mark = f"<{n.fret}>"
            elif tech in ("h", "hammer"):
                mark = f"h{n.fret}"
            elif tech in ("p", "pull"):
                mark = f"p{n.fret}"
            elif tech in ("slide", "s"):
                mark = f"/{n.fret}"
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
