"""Chords <-> lyrics: alignment, parsing existing chord sheets, rendering."""

from __future__ import annotations

import re

from arpeggio.schema import Chords, Lyrics
from arpeggio.theory import is_chord_symbol


def align_chords_with_lyrics(chords: Chords, lyrics: Lyrics, lead: float = 0.25) -> dict:
    """Attach each chord change to the word being sung when it happens.

    A chord starting slightly before a word (within `lead` seconds) is attached
    to that word, matching how chord sheets are written. Chord changes during
    instrumental gaps become `instrumental` lines.
    """
    segs = [c for c in chords.chords if c.chord != "N"]
    lines_out = []
    used = set()
    all_words = [(li, wi, w) for li, line in enumerate(lyrics.lines) for wi, w in enumerate(line.words)]

    for li, line in enumerate(lyrics.lines):
        placements = []
        for ci, c in enumerate(segs):
            if ci in used:
                continue
            if c.start > line.end + 0.05 or c.end < line.start - lead:
                continue
            # chord already sounding when the line starts -> attach to first word
            target_t = max(c.start, line.start)
            wi = _word_at(line.words, target_t, lead)
            if wi is None:
                continue
            placements.append({"chord": c.chord, "word_index": wi, "time": round(c.start, 3)})
            used.add(ci)
        # keep at most one chord per word (the latest wins; earlier ones go before)
        dedup = {}
        for p in placements:
            dedup.setdefault(p["word_index"], []).append(p)
        final = []
        for wi, ps in sorted(dedup.items()):
            final.append({**ps[-1], "passing": [p["chord"] for p in ps[:-1]]} if len(ps) > 1 else ps[0])
        # if no chord was placed but one is sounding, show it on the first word
        if not final:
            sounding = [c for c in segs if c.start <= line.start < c.end]
            if sounding:
                final.append({"chord": sounding[0].chord, "word_index": 0,
                              "time": round(sounding[0].start, 3), "continued": True})
        lines_out.append({"text": line.text, "start": line.start, "end": line.end,
                          "words": [w.text for w in line.words], "chords": final})

    # instrumental chord runs between lines
    instrumental = []
    for ci, c in enumerate(segs):
        if ci in used:
            continue
        inside = any(line.start - lead <= c.start <= line.end for line in lyrics.lines)
        if not inside:
            instrumental.append({"chord": c.chord, "time": round(c.start, 3)})
    del all_words
    return {"kind": "chord_sheet", "lines": lines_out, "instrumental": instrumental}


def _word_at(words, t: float, lead: float) -> int | None:
    for i, w in enumerate(words):
        if w.start - lead <= t < w.end:
            return i
        if t < w.start - lead:
            return i
    return len(words) - 1 if words else None


def render_text(sheet: dict, title: str | None = None) -> str:
    """Render chords above lyrics (monospace)."""
    out = [title, ""] if title else []
    timeline = [("line", ln["start"], ln) for ln in sheet["lines"]] + \
               [("inst", c["time"], c) for c in sheet.get("instrumental", [])]
    timeline.sort(key=lambda x: x[1])
    pending_inst: list[str] = []
    for kind, _, item in timeline:
        if kind == "inst":
            pending_inst.append(item["chord"])
            continue
        if pending_inst:
            out.append("[Instrumental] " + " | ".join(_dedupe(pending_inst)))
            out.append("")
            pending_inst = []
        words = item["words"]
        starts, pos = [], 0
        for w in words:
            starts.append(pos)
            pos += len(w) + 1
        chord_line = ""
        for p in item["chords"]:
            label = " ".join(p.get("passing", []) + [p["chord"]])
            col = starts[min(p["word_index"], len(starts) - 1)] if starts else 0
            if len(chord_line) > col:
                col = len(chord_line) + 1
            chord_line = chord_line.ljust(col) + label
        lyric_line = " ".join(words)
        # widen lyric line when chords overflow it
        out.append(chord_line.rstrip())
        out.append(lyric_line)
    if pending_inst:
        out.append("[Instrumental] " + " | ".join(_dedupe(pending_inst)))
    return "\n".join(out).rstrip() + "\n"


def render_chordpro(sheet: dict, title: str | None = None, artist: str | None = None) -> str:
    out = []
    if title:
        out.append(f"{{title: {title}}}")
    if artist:
        out.append(f"{{artist: {artist}}}")
    for ln in sheet["lines"]:
        words = list(ln["words"])
        for p in sorted(ln["chords"], key=lambda p: -p["word_index"]):
            i = min(p["word_index"], len(words) - 1)
            words[i] = "".join(f"[{c}]" for c in p.get("passing", [])) + f"[{p['chord']}]" + words[i]
        out.append(" ".join(words))
    return "\n".join(out) + "\n"


def _dedupe(seq):
    out = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(s)
    return out


_INLINE = re.compile(r"\[([^\]]+)\]")


def parse_chord_sheet(text: str) -> dict:
    """Parse chords-over-lyrics or ChordPro text into structured lines.

    Returns {"lines": [{"text", "chords": [{"chord", "char"}], "section"}]}
    where `char` is the character offset in the lyric line.
    """
    lines = text.splitlines()
    out, section = [], None
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        m = re.fullmatch(r"\{(?:c|comment|start_of_\w+|soc|sov)[:\s]*([^}]*)\}|\[([A-Za-z \-]+\d*)\]|"
                         r"([A-Za-z \-]+\d*):", stripped)
        if m and not is_chord_symbol(stripped.strip("[]")):
            section = next(g for g in m.groups() if g is not None).strip() or section
            i += 1
            continue
        if stripped.startswith("{"):
            i += 1
            continue
        if _INLINE.search(raw):  # ChordPro inline
            chords, text_out, pos = [], "", 0
            for part in _INLINE.split(raw):
                if pos % 2 == 1 and is_chord_symbol(part.strip()):
                    chords.append({"chord": part.strip(), "char": len(text_out)})
                else:
                    text_out += part
                pos += 1
            out.append({"text": text_out.strip(), "chords": chords, "section": section})
            i += 1
            continue
        if _is_chord_line(raw):
            chords = [{"chord": m.group(0), "char": m.start()} for m in re.finditer(r"\S+", raw)
                      if is_chord_symbol(m.group(0))]
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip() and not _is_chord_line(nxt):
                out.append({"text": nxt.rstrip(), "chords": chords, "section": section})
                i += 2
            else:
                out.append({"text": "", "chords": chords, "section": section, "instrumental": True})
                i += 1
            continue
        out.append({"text": raw, "chords": [], "section": section})
        i += 1
    return {"kind": "parsed_chord_sheet", "lines": out,
            "chord_vocabulary": sorted({c["chord"] for ln in out for c in ln["chords"]})}


def _is_chord_line(line: str) -> bool:
    toks = [t for t in re.split(r"[\s|]+", line.strip()) if t and t not in ("-", "x2", "x3", "x4")]
    return bool(toks) and all(is_chord_symbol(t) for t in toks)
