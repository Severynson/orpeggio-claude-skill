"""Unit tests for the symbolic part of Arpeggio (no ML models, no network)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arpeggio.analysis.chordsheet import align_chords_with_lyrics, parse_chord_sheet, render_text
from arpeggio.analysis.harmony import analyze_harmony, roman_numeral
from arpeggio.analysis.quantize import quantize
from arpeggio.analysis.voices import extract_voices
from arpeggio.arrangement.draft import draft_arrangement
from arpeggio.arrangement.instruments import Fretboard
from arpeggio.arrangement.tablature import chord_voicings, optimize
from arpeggio.arrangement.textfmt import from_text, patch, to_text
from arpeggio.arrangement.validate import validate
from arpeggio.export.events import build_events
from arpeggio.export.gp5 import export_gp5, read_back
from arpeggio.export.musicxml import export_musicxml
from arpeggio.export.simple import export_midi, export_pdf, render_ascii
from arpeggio.schema import (
    ChordSegment, Chords, LyricLine, Lyrics, Provenance, Rhythm, Source, TimedNote, Track,
    Transcription, Word,
)
from arpeggio.theory import harte_to_symbol, key_fifths, name_to_midi, parse_chord, transpose_chord

ATF = """\
@title Test Tune
@instrument classical_guitar
@tuning standard
@difficulty intermediate
@tempo 90
@time 4/4
@key A minor
@section Intro 1-2

M 1 Am
0    bass A2 2
0    mel  E4 1
1    mel  C5 .5
1.5  harm A3,C4 .5
2    bass E2 2
2    mel  B4 2
M 2 F | G@2
0    bass F2 2
0    mel  A4 1.5
1.5  mel  G4 .5
2    bass G2 2
2    mel  B4:1/7 2 lock
"""


def _arr():
    return from_text(ATF)


# ---------------------------------------------------------------- theory


def test_theory_basics():
    assert name_to_midi("E2") == 40 and name_to_midi("C#4") == 61 and name_to_midi("Bb3") == 58
    assert parse_chord("Am7")["pitch_classes"] == [0, 4, 7, 9]
    assert parse_chord("C/E")["bass_pc"] == 4
    assert harte_to_symbol("A:min") == "Am" and harte_to_symbol("C:maj/3") == "C/E"
    assert harte_to_symbol("G:7") == "G7" and harte_to_symbol("N") == "N"
    assert transpose_chord("Am", 3) == "Cm" and transpose_chord("Bb", 2) == "C"
    assert key_fifths("A minor") == (0, True) and key_fifths("D major") == (2, False)
    assert roman_numeral("E7", "A minor") == "V7"


# ---------------------------------------------------------------- text format


def test_atf_roundtrip():
    arr = _arr()
    assert arr.title == "Test Tune" and arr.instrument == "classical_guitar"
    assert len(arr.notes) == 12 and len(arr.chords) == 3
    assert arr.sections[0].name == "Intro"
    locked = [n for n in arr.notes if n.locked]
    assert locked and locked[0].string == 1 and locked[0].fret == 7
    again = from_text(to_text(arr))
    assert len(again.notes) == len(arr.notes)
    assert sorted(name_to_midi(n.pitch) for n in again.notes) == sorted(name_to_midi(n.pitch) for n in arr.notes)


def test_atf_sharp_not_comment_and_errors():
    arr = from_text("M 1 F#m\n0 mel F#4 1 // comment\n# full comment\n")
    assert name_to_midi(arr.notes[0].pitch) == 66
    with pytest.raises(ValueError, match="line 1"):
        from_text("0 mel E4 1\n")


def test_patch_replaces_measures():
    arr = _arr()
    sec = from_text("M 2 Dm\n0 mel D4 4\n", arr)
    out = patch(arr, sec)
    m2 = [n for n in out.notes if n.measure == 2]
    assert len(m2) == 1 and name_to_midi(m2[0].pitch) == 62
    assert [c.chord for c in out.chords if c.measure == 2] == ["Dm"]


# ---------------------------------------------------------------- tablature


def test_fretboard_positions():
    fb = Fretboard.from_spec("standard")
    assert (6, 0) in fb.positions(40) and fb.positions(39) == []
    assert (1, 0) in fb.positions(64) and (2, 5) in fb.positions(64)
    capo = Fretboard.from_spec("standard", capo=2)
    assert capo.string_pitch(6) == 42 and (6, 0) in capo.positions(42)


def test_optimize_and_validate():
    arr, report = optimize(_arr())
    assert report["assigned"] == len(arr.notes)
    fb = Fretboard.from_spec(arr.tuning, arr.capo, arr.instrument)
    for n in arr.notes:
        assert fb.pitch_at(n.string, n.fret) == name_to_midi(n.pitch)
    locked = [n for n in arr.notes if n.locked][0]
    assert (locked.string, locked.fret) == (1, 7)
    v = validate(arr)
    assert v["valid"], v["issues"]


def test_validate_catches_problems():
    arr = from_text("M 1\n0 mel E4:2/9 1\n0 bass E2:6/1 1\n0 harm G3:2/0 1\n")
    v = validate(arr)
    codes = {i["code"] for i in v["issues"]}
    assert not v["valid"]
    assert "pitch_mismatch" in codes and "string_collision" in codes


def test_optimizer_repairs_impossible_chord():
    # 7 notes at once cannot be played on 6 strings: optimizer must drop accompaniment, keep melody
    arr = from_text("M 1\n0 mel E5 1\n0 harm C4,E4,G4,C5,G3 1\n0 bass C3 1\n")
    out, report = optimize(arr)
    assert any(c["action"] in ("drop", "octave_shift", "drop_duplicate") for c in report["changes"])
    assert any(n.role == "melody" and name_to_midi(n.pitch) == 76 for n in out.notes)
    assert validate(out)["errors"] == 0


def test_chord_voicings():
    fb = Fretboard.from_spec("standard")
    shapes = [v["shape"] for v in chord_voicings("C", fb)]
    assert "x32010" in shapes
    assert "022000" in [v["shape"] for v in chord_voicings("Em", fb)]


# ---------------------------------------------------------------- export


def test_events_fill_measures():
    arr, _ = optimize(_arr())
    count, ev = build_events(arr)
    assert count == 2
    for v in (1, 2):
        for m in (1, 2):
            assert sum(e.duration for e in ev[v][m]) == 4


def test_exports(tmp_path: Path):
    arr, _ = optimize(_arr())
    gp = export_gp5(arr, tmp_path / "t.gp5")
    back = read_back(gp)
    assert back["measures"] == 2 and back["notes"] >= 12
    xml = export_musicxml(arr, tmp_path / "t.musicxml")
    import music21

    sc = music21.converter.parse(str(xml))
    assert len(sc.parts) >= 1  # notation + TAB may parse as PartStaffs
    assert len(list(sc.recurse().notes)) > 0
    mid = export_midi(arr, tmp_path / "t.mid")
    import pretty_midi

    assert len(pretty_midi.PrettyMIDI(str(mid)).instruments[0].notes) == 12
    tab = render_ascii(arr)
    assert "e|" in tab and "Am" in tab
    pdf = export_pdf(arr, tmp_path / "t.pdf")
    assert Path(pdf["path"]).read_bytes().startswith(b"%PDF")


# ---------------------------------------------------------------- analysis


def _fake_song():
    beats = [i * 0.5 for i in range(33)]  # 120 bpm, 8 bars
    src = Source(path="fake.wav")
    rh = Rhythm(source=src, provenance=Provenance(backend="test"), tempo_bpm=120, beats_per_bar=4,
                time_signature="4/4", beats=beats, downbeats=beats[::4])
    mel = [TimedNote(pitch=p, start=i * 0.5, end=i * 0.5 + 0.45) for i, p in
           enumerate([76, 74, 72, 71, 69, 71, 72, 74] * 4)]
    bass = [TimedNote(pitch=p, start=i * 2.0, end=i * 2.0 + 1.9) for i, p in enumerate([45, 41, 48, 43] * 2)]
    tr = Transcription(source=src, provenance=Provenance(backend="test"),
                       tracks=[Track(id="voice", instrument="voice", notes=mel),
                               Track(id="electric_bass", instrument="electric_bass", notes=bass)])
    ch = Chords(source=src, provenance=Provenance(backend="test"),
                chords=[ChordSegment(chord=c, start=i * 2.0, end=i * 2.0 + 2) for i, c in
                        enumerate(["Am", "F", "C", "G"] * 2)])
    return tr, rh, ch


def test_quantize_voices_harmony_draft():
    tr, rh, ch = _fake_song()
    sc = quantize(tr, rh, chords=ch)
    assert sc.measure_count == 8 and sc.key in ("A minor", "C major")
    m1 = [n for n in sc.tracks[0].notes if n.measure == 1]
    assert [n.beat for n in m1] == [0, 1, 2, 3]
    assert sc.chords[0].chord == "Am" and sc.chords[1].measure == 2
    vo = extract_voices(sc)
    assert vo.tracks[0].model_extra["source_tracks"] == ["voice"]
    assert len(vo.tracks[0].notes) == 32
    h = analyze_harmony(sc)
    assert h["agreement_detected_vs_notes"] is not None
    arr, info = draft_arrangement(vo, style="arpeggio", instrument="classical_guitar")
    assert info["melody_notes"] == 32 and info["bass_notes"] >= 8
    arr2, rep = optimize(arr)
    assert rep["assigned"] == len(arr2.notes)
    assert validate(arr2)["errors"] == 0


def test_chord_sheet_alignment_and_parse():
    src = Source(path="x")
    ch = Chords(source=src, provenance=Provenance(backend="t"),
                chords=[ChordSegment(chord="Am", start=0, end=2), ChordSegment(chord="F", start=2, end=4)])
    words = [Word(text=t, start=s, end=s + 0.4) for t, s in
             [("The", 0.1), ("stars", 0.6), ("are", 1.2), ("shining", 2.05)]]
    ly = Lyrics(source=src, provenance=Provenance(backend="t"),
                lines=[LyricLine(text="The stars are shining", start=0.1, end=2.45, words=words)])
    sheet = align_chords_with_lyrics(ch, ly)
    text = render_text(sheet)
    chord_line, lyric_line = text.splitlines()[:2]
    assert chord_line.startswith("Am") and lyric_line == "The stars are shining"
    assert chord_line.index("F") == lyric_line.index("shining")
    parsed = parse_chord_sheet("[Verse]\nAm      F\nThe stars are shining\n[C]Across the [G]sky\n")
    assert parsed["chord_vocabulary"] == ["Am", "C", "F", "G"]
    assert parsed["lines"][0]["section"] == "Verse" and parsed["lines"][0]["chords"][1]["char"] == 8


# ---------------------------------------------------------------- CLI


def test_cli_end_to_end(tmp_path: Path):
    atf = tmp_path / "a.atf"
    atf.write_text(ATF)

    def run(*args):
        r = subprocess.run([sys.executable, "-m", "arpeggio.cli", *map(str, args)], capture_output=True,
                           text=True, cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    out = json.loads(run("import-arrangement", atf, "-o", tmp_path / "a.json"))
    assert out["ok"] and out["notes"] == 12
    json.loads(run("optimize-tablature", tmp_path / "a.json"))
    v = json.loads(run("validate-arrangement", tmp_path / "a.json"))
    assert v["valid"]
    e = json.loads(run("export", tmp_path / "a.json", "--format", "gp5,ascii,midi"))
    assert Path(e["files"]["gp5"]["path"]).exists()
    assert "M 1 Am" in run("export-text", tmp_path / "a.json", "--measures", "1-1")
    assert "|" in run("render-tab", tmp_path / "a.json")


def test_musicxml_concert_pitch_and_tab_positions(tmp_path: Path):
    """MuseScore reads <pitch> as concert pitch under a treble-8vb clef; TAB frets must match."""
    arr, _ = optimize(from_text("M 1\n0 bass E2:6/0 4\n0 mel E4:1/0 4\n"))
    xml = export_musicxml(arr, tmp_path / "o.musicxml").read_text()
    assert "<clef-octave-change>-1</clef-octave-change>" in xml
    assert "<step>E</step><octave>2</octave>" in xml and "<step>E</step><octave>4</octave>" in xml
    assert "<transpose>" not in xml
    assert "<string>6</string><fret>0</fret>" in xml and "<string>1</string><fret>0</fret>" in xml


# ---------------------------------------------------------------- techniques


def _synthetic_expression_wav(path: Path):
    import numpy as np
    import soundfile as sf

    sr = 22050
    t = np.arange(8 * sr) / sr

    def tone(f_inst, a0, a1, amp=0.3):
        seg = (t >= a0) & (t < a1)
        ph = np.cumsum(f_inst) / sr
        s = sum(amp / k * np.sin(2 * np.pi * k * ph) for k in (1, 2, 3))
        return np.where(seg, s * np.minimum(1, (t - a0) * 20) * np.exp(-(t - a0) * 0.8), 0)

    hz = lambda m: 440 * 2 ** ((m - 69) / 12)  # noqa: E731
    y = tone(np.full_like(t, hz(64)), 0.5, 2.0)
    y += tone(hz(67) * 2 ** (40 * np.sin(2 * np.pi * 6 * (t - 3)) / 1200 * (t > 3.3)), 3.0, 4.6)
    y += tone(hz(69) * 2 ** (np.clip((t - 6.1) / 0.15, 0, 1) * 200 / 1200), 6.0, 7.5)
    for m in (40, 47, 52):
        y += tone(np.full_like(t, hz(m)), 0, 8, 0.25)
    sf.write(path, y + 0.01 * np.random.default_rng(0).standard_normal(len(t)), sr)


def test_detect_expression_synthetic(tmp_path: Path):
    from arpeggio.transcription.expression import detect_expression

    wav = tmp_path / "e.wav"
    _synthetic_expression_wav(wav)
    tr = Transcription(source=Source(path="x"), provenance=Provenance(backend="t"), tracks=[
        Track(id="g", instrument="g", notes=[TimedNote(pitch=64, start=0.5, end=2.0),
                                             TimedNote(pitch=67, start=3.0, end=4.6),
                                             TimedNote(pitch=69, start=6.0, end=7.5)])])
    res = detect_expression(wav, tr)
    by_pitch = {n["pitch"]: n for n in res["notes"]}
    assert 64 not in by_pitch  # steady note is not flagged
    assert 5.5 <= by_pitch[67]["vibrato"]["rate_hz"] <= 6.5
    assert by_pitch[69]["bend"]["cents"] == 200


def test_suggest_techniques_rules():
    from arpeggio.arrangement import techniques as T

    arr, _ = optimize(from_text(
        "M 1 Am\n0 bass A2 4\n0 mel C5:1/8 1\n1 mel D5:1/10 3 lock\n"     # long exposed fretted note -> vibrato
        "M 2 Am\n0 bass A2 4\n0 mel E4:1/0 4\n"                          # open string -> never vibrato
        "M 3 Am\n0 bass A2 1\n0 mel C5:2/13 .5\n.5 mel D5:2/15 .5\n"     # quick step on one string -> h
        "1 mel C5:2/13 3\n2 bass C3 1\n3 bass E3 1\n"                    # moving fretted bass -> busy hand
        "@difficulty advanced\n"))
    sugg = T.suggest(arr)
    got = {(s["measure"], s["beat"], s["technique"]): s for s in sugg}
    assert (1, 1.0, "vibrato") in got
    assert not any(s["measure"] == 2 and s["technique"] == "vibrato" for s in sugg)
    assert (3, 0.5, "h") in got and (3, 1.0, "p") in got
    busy = [s for s in sugg if s["measure"] == 3 and s["technique"] == "vibrato"]
    assert all(s["confidence"] < 0.5 for s in busy)
    applied = T.apply(sugg, "safe")
    assert applied >= 3
    assert validate(arr)["errors"] == 0


def test_technique_checks_and_exports(tmp_path: Path):
    arr, _ = optimize(from_text(
        "M 1\n0 mel E4:1/0 2 tech=vibrato\n2 mel G4:1/3 1 tech=p\n3 mel A4:1/5 1 tech=h+vibrato\n"))
    codes = {i["code"] for i in validate(arr)["issues"]}
    assert "open_string_effect" in codes and "pull_direction" in codes
    good, _ = optimize(from_text(
        "M 1\n0 mel G#4:1/4 1\n1 mel A4:1/5 3 tech=h+vibrato\nM 2\n0 mel B4:1/7 1.5\n1.5 mel D5:1/10 2.5 tech=slide\n"))
    assert validate(good)["errors"] == 0 and validate(good)["warnings"] == 0
    gp = export_gp5(good, tmp_path / "t.gp5")
    import guitarpro

    notes = [n for m in guitarpro.parse(str(gp)).tracks[0].measures for v in m.voices for b in v.beats
             for n in b.notes]
    assert notes[0].effect.hammer and notes[1].effect.vibrato  # hammer is marked on the origin note
    assert guitarpro.SlideType.legatoSlideTo in notes[2].effect.slides
    xml = export_musicxml(good, tmp_path / "t.musicxml").read_text()
    assert "vib.</words>" in xml and '<hammer-on type="start">' in xml and '<slide type="start"' in xml
    assert "<wavy-line" not in xml  # MuseScore would import it as a trill
    tab = render_ascii(good)
    assert "h5~" in tab and "/10" in tab
    import pretty_midi

    mid = pretty_midi.PrettyMIDI(str(export_midi(good, tmp_path / "t.mid")))
    assert sum(len(i.pitch_bends) for i in mid.instruments) > 20


def test_late_vibrato_keeps_moving_bass(tmp_path: Path):
    from arpeggio.arrangement import techniques as T
    from arpeggio.export.simple import export_midi

    src = ("M 1 G5\n0 bass G2:6/3 1\n0 mel A4:1/5 4\n1 bass G2:6/3 1\n2 bass A#2:5/1 1\n3 bass B2:5/2 1\n"
           "M 2 G5\n0 bass G2:6/3 4\n0 mel G#4:1/4 1\n1 mel G#4:1/4 1\n@tempo 130\n")
    arr, _ = optimize(from_text(src))
    sugg = T.suggest(arr)
    late = [s for s in sugg if s["measure"] == 1 and s["pitch"] == "A4"]
    assert late and late[0]["technique"] == "late_vibrato" and late[0]["confidence"] >= 0.5
    assert not T.suggest(arr, exclude={"vibrato"}) or all(
        "vibrato" not in s["technique"] for s in T.suggest(arr, exclude={"vibrato"}))
    T.apply(sugg, "safe")
    before = sorted((n.measure, n.beat, n.pitch, n.duration) for n in arr.notes)
    assert validate(arr)["errors"] == 0
    a4 = next(n for n in arr.notes if name_to_midi(n.pitch) == 69)
    assert T.late_vibrato_offset(a4, arr.notes, 4.0) == 3.0  # after the last bass change
    # notation: plain dotted half tied to a quarter with vibrato; nothing else changes
    exp = T.expand_late_vibrato(arr)
    pieces = sorted((n.beat, n.duration, n.tie, n.technique) for n in exp.notes if name_to_midi(n.pitch) == 69)
    assert pieces == [(0.0, 3.0, False, None), (3.0, 1.0, True, "vibrato")]
    assert sorted((n.measure, n.beat, n.pitch, n.duration) for n in arr.notes) == before
    import guitarpro

    gpn = [n for m in guitarpro.parse(str(export_gp5(arr, tmp_path / "l.gp5"))).tracks[0].measures
           for v in m.voices for b in v.beats for n in b.notes if n.string == 1]
    assert [(n.type.name, n.effect.vibrato) for n in gpn[:2]] == [("normal", False), ("tie", True)]
    assert "(~)" in render_ascii(arr)
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(export_midi(arr, tmp_path / "l.mid")))
    bends = [p.time for i in pm.instruments for p in i.pitch_bends if p.pitch]
    assert bends and min(bends) >= 3 * 60 / 130 - 1e-3  # wobble starts on beat 4


LYRICS_TEXT = """[Verse 1]
Hello bright world
You might also like
Some Other Song
[Chorus]
Sing it loud
"""


def _stt(words):
    from arpeggio.schema import LyricLine, Lyrics, Provenance, Source, Word

    ws = [Word(text=t, start=a, end=b) for t, a, b in words]
    return Lyrics(source=Source(path="x.wav"), provenance=Provenance(backend="elevenlabs"),
                  lines=[LyricLine(text=" ".join(w.text for w in ws), start=ws[0].start, end=ws[-1].end, words=ws)])


def test_lyrics_merge_prefers_user_text():
    from arpeggio.analysis.lyrics_merge import clean_lyrics_text, merge_lyrics, retime_with_vocals

    lines = clean_lyrics_text(LYRICS_TEXT)
    assert [l for _, l in lines] == ["Hello bright world", "Sing it loud"]  # headers + site junk dropped
    # misheard "bite" -> "bright", "word" -> "world"; "it" missing in the transcript
    stt = _stt([("Hello", 1.0, 1.4), ("bite", 1.5, 1.9), ("word", 2.0, 2.6), ("Sing", 5.0, 5.3), ("loud", 5.9, 6.4)])
    merged, rep = merge_lyrics(stt, LYRICS_TEXT)
    words = [(w.text, w.start, w.source) for l in merged.lines for w in l.words]
    assert [w[0] for w in words] == ["Hello", "bright", "world", "Sing", "it", "loud"]
    assert words[1][1] == 1.5 and words[1][2] == "corrected" and words[2][1] == 2.0
    assert words[4][2] == "interpolated" and 5.0 < words[4][1] < 5.9
    assert rep["corrected"] >= 1 and rep["corrections"][0]["heard"] == "bite"
    # a collapsed line (word lasting 40 s) moves into an unclaimed voice segment
    bad = _stt([("Hello", 1.0, 1.4), ("bright", 1.5, 1.9), ("world", 2.0, 2.6), ("Sing", 5.0, 45.0),
                ("it", 45.1, 45.3), ("loud", 45.4, 45.9)])
    ws = bad.lines[0].words
    bad.lines = [bad.lines[0].model_copy(update={"words": ws[:3], "start": 1.0, "end": 2.6}),
                 bad.lines[0].model_copy(update={"words": ws[3:], "start": 5.0, "end": 45.9})]
    fixed, changes = retime_with_vocals(bad, [(1.0, 2.7), (8.0, 9.5), (30.0, 30.5)])
    assert changes and 8.0 <= fixed.lines[1].start < 8.6


def test_lyrics_attach_and_export(tmp_path: Path):
    import guitarpro
    import pretty_midi

    from arpeggio.arrangement.lyrics import attach_lyrics
    from arpeggio.export.simple import export_midi
    from arpeggio.schema import Score

    arr, _ = optimize(from_text(
        "M 1 C\n0 mel E4 1\n1 mel G4 1\n2 mel C5 2\n0 bass C3 4\n"
        "M 2 G\n0 mel D5 1\n1 mel B4 3\n0 bass G2 2\n2 bass D3 2\n@tempo 120\n"))
    score = Score(tempo_bpm=120, measure_starts=[10.0, 12.0, 14.0], measure_count=2)
    ly = _stt([("Привет", 10.0, 10.4), ("мир", 10.5, 10.9), ("снова", 12.0, 12.4), ("здесь", 12.6, 13.0)])
    arr, info = attach_lyrics(arr, ly, score, bar_offset=0)
    assert info["words"] == 4 and [(w.measure, w.beat) for w in arr.lyrics][:2] == [(1, 0.0), (1, 1.0)]
    # "здесь" at 12.6 s -> bar 2 beat 1.2, inside the held B4 (voice 1): GP joins it onto "снова"
    p = export_gp5(arr, tmp_path / "l.gp5")
    song = guitarpro.parse(str(p), encoding="cp1251")
    assert song.lyrics.trackChoice == 1 and song.lyrics.lines[0].startingMeasure == 1
    assert song.lyrics.lines[0].lyrics.split(" ")[:2] == ["Привет", "мир"]
    p2 = export_gp5(arr, tmp_path / "t.gp5", lyrics_track=True)
    s2 = guitarpro.parse(str(p2), encoding="cp1251")
    assert len(s2.tracks) == 2 and s2.lyrics.trackChoice == 2
    xml = export_musicxml(arr, tmp_path / "l.musicxml").read_text(encoding="utf-8")
    assert xml.count("<lyric ") == 4 and "Привет" in xml
    assert "<lyric" not in export_musicxml(arr, tmp_path / "n.musicxml", lyrics=False).read_text(encoding="utf-8")
    pm = pretty_midi.PrettyMIDI(str(export_midi(arr, tmp_path / "l.mid")))
    assert len(pm.lyrics) == 4 and abs(pm.lyrics[1].time - 0.5) < 1e-6
    assert "Привет" in render_ascii(arr) and "Привет" not in render_ascii(arr, lyrics=False)
    gp_plain = guitarpro.parse(str(export_gp5(arr, tmp_path / "p.gp5", lyrics=False)))
    assert not gp_plain.lyrics.lines[0].lyrics
