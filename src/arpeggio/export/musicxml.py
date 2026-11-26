"""MusicXML export: one guitar part with a notation staff (treble 8vb) and a TAB staff."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape

from arpeggio.arrangement.instruments import MIDI_PROGRAMS, Fretboard
from arpeggio.export.events import build_events
from arpeggio.schema import Arrangement
from arpeggio.theory import key_fifths, parse_chord, prefers_flats

DIV = 24  # divisions per quarter
TYPE_NAMES = {1: "whole", 2: "half", 4: "quarter", 8: "eighth", 16: "16th", 32: "32nd"}
STEPS_SHARP = [("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0), ("F", 1), ("G", 0),
               ("G", 1), ("A", 0), ("A", 1), ("B", 0)]
STEPS_FLAT = [("C", 0), ("D", -1), ("D", 0), ("E", -1), ("E", 0), ("F", 0), ("G", -1), ("G", 0),
              ("A", -1), ("A", 0), ("B", -1), ("B", 0)]
KIND = {"": "major", "m": "minor", "7": "dominant", "maj7": "major-seventh", "m7": "minor-seventh",
        "dim": "diminished", "aug": "augmented", "sus4": "suspended-fourth", "sus2": "suspended-second",
        "m7b5": "half-diminished", "dim7": "diminished-seventh", "6": "major-sixth", "m6": "minor-sixth",
        "9": "dominant-ninth", "maj9": "major-ninth", "m9": "minor-ninth", "5": "power",
        "mMaj7": "major-minor", "7sus4": "suspended-fourth", "add9": "major", "madd9": "minor"}


def _pitch_xml(midi: int, flats: bool, written_octave_up: bool) -> str:
    step, alter = (STEPS_FLAT if flats else STEPS_SHARP)[midi % 12]
    octave = midi // 12 - 1 + (1 if written_octave_up else 0)
    alt = f"<alter>{alter}</alter>" if alter else ""
    return f"<pitch><step>{step}</step>{alt}<octave>{octave}</octave></pitch>"


def _harmony(symbol: str) -> str:
    try:
        c = parse_chord(symbol)
    except ValueError:
        return ""
    root = c["root"]
    step, alter = root[0], (1 if "#" in root else -1 if "b" in root[1:] else 0)
    alt = f"<root-alter>{alter}</root-alter>" if alter else ""
    kind = KIND.get(c["quality"], "major")
    bass = ""
    if c["bass"]:
        b = c["bass"]
        balt = 1 if "#" in b else -1 if "b" in b[1:] else 0
        bass = f"<bass><bass-step>{b[0]}</bass-step>" + (f"<bass-alter>{balt}</bass-alter>" if balt else "") + "</bass>"
    text = escape(c["quality"])
    return (f'<harmony print-frame="no"><root><root-step>{step}</root-step>{alt}</root>'
            f'<kind text="{text}">{kind}</kind>{bass}</harmony>')


def _legato_origins(arr: Arrangement) -> dict[int, set[str]]:
    """id(origin note) -> legato kinds starting on it (h/p/slide marked on the target)."""
    from arpeggio.arrangement.tablature import measure_quarters
    from arpeggio.arrangement.techniques import previous_on_string, tokens

    mlen = measure_quarters(arr.time_signature)
    out: dict[int, set[str]] = {}
    for n in arr.notes:
        toks = tokens(n)
        kinds = {t for t in toks if t in ("h", "p", "slide")}
        if not kinds:
            continue
        prev = previous_on_string(arr.notes, n, mlen)
        if prev is not None:
            out.setdefault(id(prev), set()).update(kinds)
    return out


def _technique_xml(n, ref, origins) -> tuple[str, str]:
    """(inside <technical>, other <notations> children) for one note piece."""
    from arpeggio.arrangement.techniques import tokens

    toks = tokens(n)
    tech, other = "", ""
    first_piece = not ref.tie_from_prev or n.tie
    last_piece = not ref.tie_to_next
    if last_piece:
        for k in sorted(origins.get(id(n), ())):
            if k == "h":
                tech += '<hammer-on type="start">H</hammer-on>'
                other += '<slur type="start" number="2"/>'
            elif k == "p":
                tech += '<pull-off type="start">P</pull-off>'
                other += '<slur type="start" number="2"/>'
            elif k == "slide":
                other += '<slide type="start" line-type="solid"/>'
    if first_piece:
        if "h" in toks:
            tech += '<hammer-on type="stop"/>'
        if "p" in toks:
            tech += '<pull-off type="stop"/>'
        if "h" in toks or "p" in toks:
            other += '<slur type="stop" number="2"/>'
        if "slide" in toks:
            other += '<slide type="stop" line-type="solid"/>'
        for t, alter in (("bend", 2), ("bend_half", 1), ("bend_release", 2)):
            if t in toks:
                tech += f"<bend><bend-alter>{alter}</bend-alter></bend>"
                if t == "bend_release":
                    tech += "<bend><bend-alter>0</bend-alter><release/></bend>"
        if "harmonic" in toks:
            tech += "<harmonic><natural/></harmonic>"
        if "palm_mute" in toks:
            tech += "<other-technical>P.M.</other-technical>"
        arts = ""
        if "staccato" in toks:
            arts += "<staccato/>"
        if "accent" in toks:
            arts += "<accent/>"
        if arts:
            other += f"<articulations>{arts}</articulations>"
    # Vibrato: MuseScore imports <wavy-line> as a trill, so we mark it the way
    # many guitar editions do — "vib." text above the staff (see _vibrato_direction)
    # and a "~" next to the fret number on the TAB staff.
    if ("vibrato" in toks or "wide_vibrato" in toks) and first_piece and n.string is not None:
        tech += f"<other-technical>{'~~' if 'wide_vibrato' in toks else '~'}</other-technical>"
    return tech, other


def _vibrato_direction(e, voice_no: int, staff: int) -> str:
    from arpeggio.arrangement.techniques import tokens

    for r in e.notes:
        toks = tokens(r.note)
        if ("vibrato" in toks or "wide_vibrato" in toks) and (not r.tie_from_prev or r.note.tie):
            word = "wide vib." if "wide_vibrato" in toks else "vib."
            return (f'<direction placement="above"><direction-type><words font-style="italic">{word}</words>'
                    f"</direction-type><voice>{voice_no}</voice><staff>{staff}</staff></direction>")
    return ""


def export_musicxml(arr: Arrangement, path: str | Path, include_tab: bool = True) -> Path:
    from arpeggio.arrangement.techniques import expand_late_vibrato

    arr = expand_late_vibrato(arr)  # delayed vibrato = plain note tied to a vibrato note
    fb = Fretboard.from_spec(arr.tuning, 0, arr.instrument)
    flats = prefers_flats(arr.key)
    num, _, den = arr.time_signature.partition("/")
    measure_count, events = build_events(arr)
    origins = _legato_origins(arr)
    fifths, minor = key_fifths(arr.key)
    staves = 2 if include_tab else 1
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
           '"http://www.musicxml.org/dtds/partwise.dtd">',
           '<score-partwise version="4.0">',
           f"<work><work-title>{escape(arr.title)}</work-title></work>",
           "<identification>"
           + (f'<creator type="composer">{escape(arr.artist)}</creator>' if arr.artist else "")
           + f'<creator type="arranger">{escape(arr.arranger)}</creator>'
           "<encoding><software>Arpeggio</software></encoding></identification>",
           '<part-list><score-part id="P1">'
           f"<part-name>{escape(arr.instrument.replace('_', ' ').title())}</part-name>"
           f'<midi-instrument id="P1-I1"><midi-channel>1</midi-channel>'
           f"<midi-program>{MIDI_PROGRAMS.get(arr.instrument, 25) + 1}</midi-program></midi-instrument>"
           "</score-part></part-list>",
           '<part id="P1">']
    mlen_div = int(Fraction(int(num) * 4, int(den or 4)) * DIV)
    for m in range(1, measure_count + 1):
        out.append(f'<measure number="{m}">')
        if m == 1:
            attrs = [f"<divisions>{DIV}</divisions>",
                     f"<key><fifths>{fifths}</fifths><mode>{'minor' if minor else 'major'}</mode></key>",
                     f"<time><beats>{num}</beats><beat-type>{den or 4}</beat-type></time>",
                     f"<staves>{staves}</staves>",
                     '<clef number="1"><sign>G</sign><line>2</line><clef-octave-change>-1</clef-octave-change></clef>']
            if include_tab:
                attrs.append('<clef number="2"><sign>TAB</sign><line>5</line></clef>')
                tuning = "".join(
                    f'<staff-tuning line="{i + 1}"><tuning-step>{_pitch_xml(fb.string_pitch(s), False, False)[13:14]}</tuning-step>'
                    + (f"<tuning-alter>{a}</tuning-alter>" if (a := STEPS_SHARP[fb.string_pitch(s) % 12][1]) else "")
                    + f"<tuning-octave>{fb.string_pitch(s) // 12 - 1}</tuning-octave></staff-tuning>"
                    for i, s in enumerate(range(fb.n_strings, 0, -1)))
                capo = f"<capo>{arr.capo}</capo>" if arr.capo else ""
                attrs.append(f'<staff-details number="2"><staff-lines>{fb.n_strings}</staff-lines>{tuning}{capo}</staff-details>')
            out.append("<attributes>" + "".join(attrs) + "</attributes>")
            out.append('<direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit>'
                       f"<per-minute>{int(round(arr.tempo_bpm))}</per-minute></metronome></direction-type>"
                       f'<sound tempo="{arr.tempo_bpm:.2f}"/></direction>')
        for s in arr.sections:
            if s.start_measure == m:
                out.append(f'<direction placement="above"><direction-type><rehearsal>{escape(s.name)}'
                           "</rehearsal></direction-type></direction>")
        staff_list = [(1, 1), (2, 1)] + ([(1, 2), (2, 2)] if include_tab else [])
        first = True
        for v, staff in staff_list:
            evs = events[v].get(m, [])
            if v == 2 and all(not e.notes for e in evs):
                continue
            if not first:
                out.append(f"<backup><duration>{mlen_div}</duration></backup>")
            first = False
            voice_no = v + (4 if staff == 2 else 0)
            for e in evs:
                if staff == 1 and e.notes:
                    out.append(_vibrato_direction(e, voice_no, staff))
                if e.chord and staff == 1 and (v == 1 or not any(x.onset == e.onset and x.chord for x in events[1].get(m, []))):
                    out.append(_harmony(e.chord))
                dur = int(e.duration * DIV)
                typ = TYPE_NAMES[e.value]
                extra = ("<dot/>" if e.dotted else "")
                tm = "<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>" if e.triplet else ""
                stem = "" if staff == 2 else f"<stem>{'up' if v == 1 else 'down'}</stem>"
                if not e.notes:
                    out.append(f"<note><rest/><duration>{dur}</duration><voice>{voice_no}</voice>"
                               f"<type>{typ}</type>{extra}{tm}<staff>{staff}</staff></note>")
                    continue
                refs = sorted(e.notes, key=lambda r: r.pitch)
                seen_strings = set()
                for i, r in enumerate(refs):
                    chord_tag = "<chord/>" if i > 0 else ""
                    ties = ""
                    tied = ""
                    if r.tie_from_prev:
                        ties += '<tie type="stop"/>'
                        tied += '<tied type="stop"/>'
                    if r.tie_to_next:
                        ties += '<tie type="start"/>'
                        tied += '<tied type="start"/>'
                    n = r.note
                    tech_inner = ""
                    if staff == (2 if include_tab else 1) and n.string is not None and n.fret is not None \
                            and n.string not in seen_strings:
                        seen_strings.add(n.string)
                        tech_inner = f"<string>{n.string}</string><fret>{n.fret}</fret>"
                    fx_tech, fx_other = _technique_xml(n, r, origins)
                    tech_all = tech_inner + fx_tech
                    tech = f"<technical>{tech_all}</technical>" if tech_all else ""
                    body = tied + fx_other + tech
                    notations = f"<notations>{body}</notations>" if body else ""
                    out.append(f"<note>{chord_tag}{_pitch_xml(r.pitch, flats, False)}<duration>{dur}</duration>"
                               f"{ties}<voice>{voice_no}</voice><type>{typ}</type>{extra}{tm}{stem}"
                               f"<staff>{staff}</staff>{notations}</note>")
        if m == measure_count:
            out.append('<barline location="right"><bar-style>light-heavy</bar-style></barline>')
        out.append("</measure>")
    out.append("</part></score-partwise>")
    path = Path(path)
    path.write_text("\n".join(out), encoding="utf-8")
    return path
