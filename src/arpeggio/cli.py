"""Arpeggio command-line interface.

Every command writes its full result to a JSON (or other) file and prints a
compact JSON summary on stdout: {"ok": true, "output": "...", ...}. Progress
goes to stderr. On failure: {"ok": false, "error": "...", "hint": "..."} and
exit code 1.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from arpeggio.artifacts import ArpeggioError, emit, emit_error, load, load_json, log, save
from arpeggio.config import load_env, work_dir_for

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False,
                  help="Arpeggio: composable music perception, analysis, arrangement and export tools.")

Out = Annotated[Optional[Path], typer.Option("--output", "-o", help="Output file (default: ./arpeggio-work/<song>/...)")]


def command(fn):
    """Register a command with uniform error handling."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ArpeggioError as e:
            emit_error(str(e))
            raise typer.Exit(1)
        except (ValueError, KeyError) as e:
            emit_error(f"{type(e).__name__}: {e}")
            raise typer.Exit(1)

    return app.command()(wrapper)


def _out(output: Optional[Path], audio_or_name: str | Path, default: str) -> Path:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        return output
    return work_dir_for(audio_or_name) / default


def _measures(spec: Optional[str]) -> tuple[int, int] | None:
    if not spec:
        return None
    a, _, b = spec.partition("-")
    return int(a), int(b or a)


def _list(spec: Optional[str]) -> list[str] | None:
    return [s.strip() for s in spec.split(",") if s.strip()] if spec else None


def _song_name(path: Path) -> str:
    """Name of the song a derived artifact belongs to (its parent work dir)."""
    return path.parent.name if path.parent.parent.name == "arpeggio-work" else path.stem


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@command
def doctor():
    """Check dependencies, model access, API keys and hardware."""
    import importlib.util
    import os
    import platform

    load_env()
    from arpeggio.config import torch_device
    from arpeggio.export.simple import find_musescore

    mods = {m: importlib.util.find_spec(m) is not None for m in
            ("muscriptor", "beat_this", "lv_chordia", "demucs", "librosa", "music21", "guitarpro", "torch")}
    hf = None
    if mods["muscriptor"]:
        import logging

        logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
        try:
            from huggingface_hub import auth_check

            auth_check("MuScriptor/muscriptor-medium")
            hf = "ok"
        except Exception as e:
            name = type(e).__name__
            hf = {"GatedRepoError": "no access: licence not accepted or HF token missing/invalid",
                  "RepositoryNotFoundError": "no access: HF token missing/invalid"}.get(name, f"no access: {name}")
            try:
                from huggingface_hub import whoami

                whoami()
            except Exception:
                hf += " (current HF token is invalid — run `uvx hf auth login --force`)"
    import shutil

    emit({"python": platform.python_version(), "platform": platform.platform(), "device": torch_device(),
          "modules": mods, "ffmpeg": shutil.which("ffmpeg"), "musescore": find_musescore(),
          "elevenlabs_key": bool(os.environ.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVEN_LABS_API_KEY")),
          "muscriptor_weights_access": hf,
          "hints": [h for h in [
              None if hf in (None, "ok") else "Accept the licence at https://huggingface.co/MuScriptor/muscriptor-medium "
              "and run `uvx hf auth login` (or use --backend pyin).",
              None if find_musescore() else "Optional: install MuseScore 4 for engraved PDF output.",
          ] if h]})


# ---------------------------------------------------------------------------
# A. Audio perception
# ---------------------------------------------------------------------------


@command
def transcribe_notes(
    audio: Path,
    output: Out = None,
    backend: Annotated[str, typer.Option(help="muscriptor | pyin")] = "muscriptor",
    model: Annotated[str, typer.Option(help="MuScriptor size: small | medium | large")] = "medium",
    instruments: Annotated[Optional[str], typer.Option(help="Comma list to keep, e.g. voice,electric_bass")] = None,
    start: Annotated[Optional[float], typer.Option(help="Start time (s)")] = None,
    end: Annotated[Optional[float], typer.Option(help="End time (s)")] = None,
    device: Annotated[Optional[str], typer.Option(help="auto | cpu | mps | cuda")] = None,
    no_cache: bool = False,
    constrain: Annotated[bool, typer.Option(help="Force MuScriptor to decode only --instruments "
                                                 "(hard constraint) instead of filtering afterwards")] = False,
    midi: Annotated[bool, typer.Option(help="Also write a .mid next to the JSON")] = True,
):
    """Audio -> instrument-specific timestamped notes (+ MIDI)."""
    from arpeggio.transcription.notes import summarize_transcription, transcribe_notes as run, write_midi

    tr = run(audio, backend=backend, model_size=model, instruments=_list(instruments), start=start,
             end=end, device=device, use_cache=not no_cache, constrain=constrain)
    out = _out(output, audio, "notes.json")
    if midi:
        tr.midi_path = str(write_midi(tr, out.with_suffix(".mid")))
    save(tr, out)
    emit({"output": str(out), "midi": tr.midi_path, **summarize_transcription(tr)})


@command
def detect_rhythm(
    audio: Path, output: Out = None,
    beats_per_bar: Annotated[Optional[int], typer.Option(help="Override the detected meter")] = None,
    backend: Annotated[str, typer.Option(help="auto | beat_this | librosa")] = "auto",
    device: Optional[str] = None,
):
    """Audio -> beats, downbeats, tempo, time signature."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.transcription.rhythm import detect_rhythm as run

    r = run(audio, backend=backend, device=device, beats_per_bar=beats_per_bar)
    out = save(r, _out(output, audio, "rhythm.json"))
    emit({"output": str(out), **summarize(r)})


@command
def detect_chords(
    audio: Path, output: Out = None,
    backend: Annotated[str, typer.Option(help="auto | lv-chordia | chroma")] = "auto",
    vocabulary: Annotated[str, typer.Option(help="lv-chordia: submission | ismir2017 | full | extended")] = "submission",
    start: Optional[float] = None, end: Optional[float] = None,
    min_duration: Annotated[float, typer.Option(help="Absorb chord blips shorter than this (s)")] = 0.3,
):
    """Audio -> timestamped chord progression."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.transcription.chords import detect_chords as run

    c = run(audio, backend=backend, vocabulary=vocabulary, start=start, end=end, min_duration=min_duration)
    out = save(c, _out(output, audio, "chords.json"))
    emit({"output": str(out), **summarize(c)})


@command
def transcribe_lyrics(
    audio: Path, output: Out = None,
    language: Annotated[Optional[str], typer.Option(help="ISO code, e.g. en, uk, de (auto if omitted)")] = None,
    isolate_vocals: Annotated[bool, typer.Option(help="Run Demucs vocal separation first")] = False,
    model: str = "scribe_v2",
    start: Optional[float] = None, end: Optional[float] = None, device: Optional[str] = None,
):
    """Audio -> lyrics with word timestamps (ElevenLabs Scribe)."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.transcription.lyrics import transcribe_lyrics as run

    ly = run(audio, language=language, isolate_vocals=isolate_vocals, model=model, start=start, end=end,
             device=device)
    out = save(ly, _out(output, audio, "lyrics.json"))
    emit({"output": str(out), **summarize(ly)})


@command
def align_lyrics(
    audio: Path,
    lyrics_file: Annotated[Path, typer.Argument(help="Plain-text lyrics, one line per sung line")],
    output: Out = None,
    isolate_vocals: bool = False,
    start: Optional[float] = None, end: Optional[float] = None, device: Optional[str] = None,
):
    """Forced alignment of known lyrics -> word timestamps (keeps the user's text)."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.transcription.lyrics import align_lyrics as run

    ly = run(audio, lyrics_file.read_text(encoding="utf-8"), isolate_vocals=isolate_vocals, start=start,
             end=end, device=device)
    out = save(ly, _out(output, audio, "lyrics.json"))
    emit({"output": str(out), **summarize(ly)})


@command
def sync_lyrics(
    audio: Path, output: Out = None,
    text: Annotated[Optional[Path], typer.Option(
        help="The user's lyrics (trusted words; [Section] headers and lyrics-site junk are stripped)")] = None,
    transcript: Annotated[Optional[Path], typer.Option(
        help="Reuse an existing transcribe-lyrics JSON instead of calling speech-to-text again")] = None,
    method: Annotated[str, typer.Option(help="auto (forced alignment + transcript) | forced | transcript")] = "auto",
    language: Annotated[Optional[str], typer.Option(help="ISO code for speech-to-text, e.g. ru, uk, en")] = None,
    isolate_vocals: Annotated[bool, typer.Option(help="Demucs vocal stem for timing + voice-activity repair")] = True,
    device: Optional[str] = None,
):
    """Timed lyrics for tabs: user text if given (always preferred), else ElevenLabs transcript.

    With --text: forced alignment of the text, the speech-to-text transcript merged
    word-by-word as a second opinion (misheard words replaced by the user's), and
    lines with collapsed timing moved into voice-active parts of the vocal stem.
    """
    from arpeggio.analysis.lyrics_merge import sync_lyrics as run
    from arpeggio.analysis.sections import summarize

    if method not in ("auto", "forced", "transcript"):
        raise ArpeggioError("--method must be auto, forced or transcript")
    tr = load(transcript, "lyrics") if transcript else None
    ly, rep = run(audio, text.read_text(encoding="utf-8") if text else None, tr, language=language,
                  isolate_vocals=isolate_vocals, method=method, device=device)
    out = save(ly, _out(output, audio, "lyrics_synced.json" if text else "lyrics.json"))
    sources = {}
    for ln in ly.lines:
        for w in ln.words:
            sources[w.source or "transcript"] = sources.get(w.source or "transcript", 0) + 1
    emit({"output": str(out), "word_sources": sources, **rep, **summarize(ly)})


@command
def attach_lyrics(
    arrangement: Path,
    lyrics: Annotated[Path, typer.Argument(help="Timed lyrics JSON (sync-lyrics / transcribe-lyrics / align-lyrics)")],
    score: Annotated[Path, typer.Option(help="score.json whose measure_starts map seconds to bars")],
    bar_offset: Annotated[int, typer.Option(help="recording bar = arrangement bar + offset")] = 0,
    output: Out = None,
):
    """Put timed lyrics onto the arrangement's bars/beats (exported with every format)."""
    from arpeggio.arrangement.lyrics import attach_lyrics as run

    arr, info = run(load(arrangement, "arrangement"), load(lyrics, "lyrics"), load(score, "score"), bar_offset)
    out = save(arr, output or arrangement)
    preview = {}
    for w in arr.lyrics:
        preview.setdefault(w.measure, []).append(w.text)
    emit({"output": str(out), **info,
          "by_measure": {m: " ".join(ws) for m, ws in list(preview.items())[:40]}})


@command
def separate(
    audio: Path,
    stems: Annotated[str, typer.Option(help="all | vocals | bass | drums | other | guitar | piano")] = "all",
    model: Annotated[str, typer.Option(help="htdemucs | htdemucs_ft | htdemucs_6s")] = "htdemucs",
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir")] = None,
    device: Optional[str] = None,
):
    """Separate instruments into WAV stems (Demucs)."""
    from arpeggio.transcription.separation import separate as run

    emit({"stems": run(audio, stems=stems, model=model, device=device, out_dir=output_dir)})


@command
def analyze_audio(
    audio: Path,
    workdir: Annotated[Optional[Path], typer.Option(help="Directory for all outputs")] = None,
    backend: str = "muscriptor", model: str = "medium",
    lyrics: Annotated[bool, typer.Option(help="Also transcribe lyrics (ElevenLabs)")] = False,
    language: Optional[str] = None,
    grid: int = 4,
    beats_per_bar: Optional[int] = None,
    start: Optional[float] = None, end: Optional[float] = None,
    device: Optional[str] = None,
):
    """One-shot perception: notes + rhythm + chords (+ lyrics) -> quantized score + voices."""
    from arpeggio.analysis.quantize import quantize
    from arpeggio.analysis.sections import summarize
    from arpeggio.analysis.voices import extract_voices
    from arpeggio.transcription.chords import detect_chords as run_chords
    from arpeggio.transcription.notes import transcribe_notes as run_notes, write_midi
    from arpeggio.transcription.rhythm import detect_rhythm as run_rhythm

    wd = workdir or work_dir_for(audio)
    wd.mkdir(parents=True, exist_ok=True)
    log("1/4 rhythm")
    rh = run_rhythm(audio, device=device, beats_per_bar=beats_per_bar)
    save(rh, wd / "rhythm.json")
    log("2/4 chords")
    ch = run_chords(audio, start=start, end=end)
    save(ch, wd / "chords.json")
    ly = None
    if lyrics:
        from arpeggio.transcription.lyrics import transcribe_lyrics as run_lyrics

        log("lyrics")
        ly = run_lyrics(audio, language=language, start=start, end=end)
        save(ly, wd / "lyrics.json")
    log("3/4 notes")
    tr = run_notes(audio, backend=backend, model_size=model, start=start, end=end, device=device)
    tr.midi_path = str(write_midi(tr, wd / "notes.mid"))
    save(tr, wd / "notes.json")
    log("4/4 quantize + voices")
    sc = quantize(tr, rh, chords=ch, lyrics=ly, grid=grid)
    save(sc, wd / "score.json")
    vo = extract_voices(sc)
    save(vo, wd / "voices.json")
    emit({"workdir": str(wd),
          "files": {k: str(wd / f) for k, f in [("rhythm", "rhythm.json"), ("chords", "chords.json"),
                                                ("notes", "notes.json"), ("midi", "notes.mid"),
                                                ("score", "score.json"), ("voices", "voices.json")]
                    + ([("lyrics", "lyrics.json")] if ly else [])},
          "score": summarize(sc),
          "voices": {"melody_from": vo.tracks[0].model_extra.get("source_tracks"),
                     "bass_from": vo.tracks[1].model_extra.get("source_tracks"),
                     "melody_notes": len(vo.tracks[0].notes), "bass_notes": len(vo.tracks[1].notes)}})


# ---------------------------------------------------------------------------
# B. Symbolic analysis
# ---------------------------------------------------------------------------


@command
def quantize(
    notes: Annotated[Path, typer.Argument(help="Transcription JSON (transcribe-notes)")],
    rhythm: Annotated[Path, typer.Option(help="Rhythm JSON (detect-rhythm)")],
    output: Out = None,
    chords: Annotated[Optional[Path], typer.Option(help="Chords JSON to place on the grid")] = None,
    lyrics: Annotated[Optional[Path], typer.Option(help="Lyrics JSON to place on the grid")] = None,
    grid: Annotated[int, typer.Option(help="Subdivisions per beat: 4=16ths, 2=8ths, 3=8th triplets, 12=both")] = 4,
    include_drums: bool = False,
):
    """Timestamped notes -> measures, beats and durations."""
    from arpeggio.analysis.quantize import quantize as run
    from arpeggio.analysis.sections import summarize

    sc = run(load(notes, "transcription"), load(rhythm, "rhythm"),
             chords=load(chords, "chords") if chords else None,
             lyrics=load(lyrics, "lyrics") if lyrics else None, grid=grid, include_drums=include_drums)
    out = save(sc, output or notes.parent / "score.json")
    emit({"output": str(out), **summarize(sc)})


@command
def extract_voices(
    score: Annotated[Path, typer.Argument(help="Score JSON (quantize)")],
    output: Out = None,
    melody: Annotated[Optional[str], typer.Option(help="Track id(s) carrying the melody (auto: voice/lead)")] = None,
    bass: Annotated[Optional[str], typer.Option(help="Track id(s) carrying the bass (auto: bass instruments)")] = None,
    melody_min: Annotated[str, typer.Option(help="Ignore melody notes below this pitch")] = "E3",
):
    """Reduce a multi-track score to one melody line + one bass line (skyline)."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.analysis.voices import extract_voices as run
    from arpeggio.theory import name_to_midi

    vo = run(load(score, "score"), _list(melody), _list(bass), melody_min_pitch=name_to_midi(melody_min))
    out = save(vo, output or score.parent / "voices.json")
    emit({"output": str(out), "melody_from": vo.tracks[0].model_extra.get("source_tracks"),
          "bass_from": vo.tracks[1].model_extra.get("source_tracks"), **summarize(vo)})


@command
def analyze_harmony(
    score: Annotated[Path, typer.Argument(help="Score JSON")],
    output: Out = None,
    tracks: Annotated[Optional[str], typer.Option(help="Tracks to analyse (default: all pitched)")] = None,
    resolution: Annotated[str, typer.Option(help="measure | half")] = "measure",
    measures: Annotated[Optional[str], typer.Option(help="Only print rows for e.g. 1-16")] = None,
):
    """Key, note-based chord candidates per measure, agreement with detected chords."""
    from arpeggio.analysis.harmony import analyze_harmony as run

    res = run(load(score, "score"), _list(tracks), resolution)
    out = save(res, output or score.parent / "harmony.json")
    rows = res["windows"]
    rng = _measures(measures)
    if rng:
        rows = [r for r in rows if rng[0] <= r["measure"] <= rng[1]]
    disagreements = [r["measure"] for r in res["windows"] if r.get("disagreement")]
    emit({"output": str(out), "key": res["key"], "key_candidates": res["key_candidates"],
          "agreement": res["agreement_detected_vs_notes"], "disagreement_measures": disagreements[:40],
          "rows": rows[:24] if not rng else rows})


@command
def align_chords_lyrics(
    chords: Path, lyrics: Path, output: Out = None,
    format: Annotated[str, typer.Option(help="text | chordpro")] = "text",
    title: Optional[str] = None,
):
    """Place chord changes above the words they fall on (chord sheet)."""
    from arpeggio.analysis.chordsheet import align_chords_with_lyrics, render_chordpro, render_text

    sheet = align_chords_with_lyrics(load(chords, "chords"), load(lyrics, "lyrics"))
    out = save(sheet, output or chords.parent / "chord_sheet.json")
    text = render_chordpro(sheet, title) if format == "chordpro" else render_text(sheet, title)
    tpath = out.with_suffix(".cho" if format == "chordpro" else ".txt")
    tpath.write_text(text, encoding="utf-8")
    emit({"output": str(out), "rendered": str(tpath), "lines": len(sheet["lines"]), "preview": text[:1500]})


@command
def parse_chord_sheet(sheet: Path, output: Out = None):
    """Parse chords-over-lyrics or ChordPro text into structured JSON."""
    from arpeggio.analysis.chordsheet import parse_chord_sheet as run

    res = run(sheet.read_text(encoding="utf-8"))
    out = save(res, output or sheet.with_suffix(".parsed.json"))
    emit({"output": str(out), "lines": len(res["lines"]), "chord_vocabulary": res["chord_vocabulary"]})


@command
def summary(artifact: Path):
    """Level-1 overview of any Arpeggio JSON artifact."""
    from arpeggio.analysis.sections import summarize

    emit({"file": str(artifact), "kind": load_json(artifact).get("kind"), **summarize(load(artifact))})


@command
def get_section(
    artifact: Path,
    measures: Annotated[Optional[str], typer.Option(help="Measure range, e.g. 9-16")] = None,
    start: Annotated[Optional[float], typer.Option(help="Start time (s)")] = None,
    end: Annotated[Optional[float], typer.Option(help="End time (s)")] = None,
    tracks: Annotated[Optional[str], typer.Option(help="Comma list of track ids")] = None,
):
    """Print a compact text slice (measures or seconds) of an artifact."""
    from arpeggio.analysis.sections import section

    sys.stdout.write(section(load(artifact), _measures(measures), start, end, _list(tracks)))


# ---------------------------------------------------------------------------
# C. Arrangement & tablature
# ---------------------------------------------------------------------------


@command
def list_tunings():
    """Known tunings and difficulty profiles."""
    from dataclasses import asdict

    from arpeggio.arrangement.instruments import DIFFICULTIES, TUNINGS

    emit({"tunings": TUNINGS, "difficulties": {k: asdict(v) for k, v in DIFFICULTIES.items()}})


@command
def chord_voicings(
    chords: Annotated[str, typer.Argument(help="Comma list: Am,F,C,G")],
    tuning: str = "standard", capo: int = 0, difficulty: str = "intermediate",
    instrument: str = "acoustic_guitar", max_results: int = 4,
):
    """Playable chord shapes (low string -> high string, x = muted)."""
    from arpeggio.arrangement.instruments import Fretboard
    from arpeggio.arrangement.tablature import chord_voicings as run

    fb = Fretboard.from_spec(tuning, capo, instrument)
    res = {c: [{"shape": v["shape"], "position": v["position"]} for v in run(c, fb, difficulty, max_results)]
           for c in _list(chords) or []}
    emit({"tuning": fb.describe(), "voicings": res})


@command
def suggest_key(
    voices: Annotated[Path, typer.Argument(help="Voices/score JSON")],
):
    """Rank transpositions by how guitar-friendly the resulting key is."""
    from arpeggio.arrangement.draft import best_transposition

    sc = load(voices, "score")
    mel = [n.pitch for t in sc.tracks if t.id == "melody" for n in t.notes]
    emit(best_transposition(sc, mel))


@command
def arrange_draft(
    voices: Annotated[Path, typer.Argument(help="Voices JSON (extract-voices) with chords")],
    output: Out = None,
    style: Annotated[str, typer.Option(help="ballad | arpeggio | travis | sparse")] = "arpeggio",
    difficulty: Annotated[str, typer.Option(help="beginner | intermediate | advanced")] = "intermediate",
    instrument: Annotated[str, typer.Option(help="classical_guitar | acoustic_guitar")] = "classical_guitar",
    tuning: str = "standard", capo: int = 0,
    transpose: Annotated[str, typer.Option(help="Semitones or 'auto'")] = "0",
    measures: Annotated[Optional[str], typer.Option(help="Only draft e.g. 1-16")] = None,
    title: str = "Untitled", artist: str = "",
):
    """Deterministic baseline fingerstyle arrangement for the agent to refine."""
    from arpeggio.arrangement.draft import draft_arrangement

    arr, info = draft_arrangement(load(voices, "score"), style=style, difficulty=difficulty,
                                  instrument=instrument, tuning=tuning, capo=capo,
                                  transpose=transpose if transpose == "auto" else int(transpose),
                                  title=title, artist=artist, measures=_measures(measures))
    out = save(arr, output or voices.parent / "arrangement.json")
    emit({"output": str(out), **info})


@command
def import_arrangement(
    text_file: Annotated[Path, typer.Argument(help="Arrangement in Arpeggio Text Format (.atf)")],
    output: Out = None,
    base: Annotated[Optional[Path], typer.Option(help="Existing arrangement JSON to take headers from")] = None,
):
    """ATF text -> arrangement JSON."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.arrangement.textfmt import from_text

    arr = from_text(text_file.read_text(encoding="utf-8"), load(base, "arrangement") if base else None)
    out = save(arr, output or text_file.with_suffix(".json"))
    emit({"output": str(out), **summarize(arr)})


@command
def export_text(
    arrangement: Path, output: Out = None,
    measures: Annotated[Optional[str], typer.Option(help="e.g. 1-8")] = None,
    positions: Annotated[bool, typer.Option(help="Include :string/fret")] = True,
):
    """Arrangement JSON -> ATF text (to read or edit compactly)."""
    from arpeggio.arrangement.textfmt import to_text

    text = to_text(load(arrangement, "arrangement"), _measures(measures), headers=True, positions=positions)
    if output:
        output.write_text(text, encoding="utf-8")
        emit({"output": str(output), "chars": len(text)})
    else:
        sys.stdout.write(text)


@command
def patch_arrangement(
    arrangement: Path,
    section_file: Annotated[Path, typer.Argument(help="ATF text (or arrangement JSON) for some measures")],
    output: Out = None,
):
    """Replace the measures contained in SECTION_FILE inside ARRANGEMENT."""
    from arpeggio.analysis.sections import summarize
    from arpeggio.arrangement.textfmt import from_text, patch

    arr = load(arrangement, "arrangement")
    if section_file.suffix == ".json":
        sec = load(section_file, "arrangement")
    else:
        sec = from_text(section_file.read_text(encoding="utf-8"), arr)
    res = patch(arr, sec)
    out = save(res, output or arrangement)
    emit({"output": str(out), "replaced_measures": sorted({n.measure for n in sec.notes} |
                                                          {c.measure for c in sec.chords}),
          **summarize(res)})


@command
def transpose(arrangement: Path, semitones: Annotated[int, typer.Option(allow_dash=True)], output: Out = None):
    """Transpose an arrangement (clears tablature; re-run optimize-tablature)."""
    from arpeggio.arrangement.draft import transpose_arrangement

    res = transpose_arrangement(load(arrangement, "arrangement"), semitones)
    out = save(res, output or arrangement)
    emit({"output": str(out), "key": res.key})


@command
def detect_expression(
    audio: Annotated[Path, typer.Argument(help="Recording or stem (a stem gives cleaner results)")],
    notes: Annotated[Path, typer.Argument(help="Transcription JSON whose notes to analyse")],
    output: Out = None,
    tracks: Annotated[Optional[str], typer.Option(help="Track ids to analyse (default: all pitched)")] = None,
    min_duration: Annotated[float, typer.Option(help="Skip notes shorter than this (s)")] = 0.3,
):
    """Measure real vibrato and bends of transcribed notes in the recording."""
    from arpeggio.transcription.expression import detect_expression as run

    res = run(audio, load(notes, "transcription"), _list(tracks), min_duration)
    out = save(res, output or notes.parent / "expression.json")
    emit({"output": str(out), "stats": res["stats"],
          "examples": res["notes"][:12]})


@command
def suggest_techniques(
    arrangement: Path,
    output: Out = None,
    expression: Annotated[Optional[Path], typer.Option(help="expression.json (detect-expression) = evidence")] = None,
    score: Annotated[Optional[Path], typer.Option(help="score.json, to map arrangement bars to recording time")] = None,
    bar_offset: Annotated[int, typer.Option(help="recording bar = arrangement bar + offset")] = 0,
    idiomatic: Annotated[bool, typer.Option(help="Also propose idiomatic vibrato/slides/legato")] = True,
    apply: Annotated[Optional[str], typer.Option(help="Apply: evidence | safe | all (default: only suggest)")] = None,
    min_confidence: Annotated[float, typer.Option(help="Threshold for --apply safe")] = 0.5,
    density_bars: Annotated[float, typer.Option(help="At most one idiomatic vibrato per N bars")] = 2.0,
    exclude: Annotated[Optional[str], typer.Option(
        help="Never suggest these, comma list: slide, bend, vibrato, legato, h, p, late_vibrato, …")] = None,
):
    """Suggest (and optionally apply) vibrato, bends, slides, hammer-ons/pull-offs.

    Never changes pitches or rhythm — only the `technique` of notes. Run after
    optimize-tablature (needs string/fret), then validate-arrangement.
    """
    from arpeggio.arrangement import techniques as T

    arr = load(arrangement, "arrangement")
    if not any(n.string is not None for n in arr.notes):
        raise ArpeggioError("arrangement has no tablature yet: run optimize-tablature first")
    expr = load_json(expression) if expression else None
    sc = load(score, "score") if score else None
    if expr and sc is None:
        raise ArpeggioError("--expression needs --score (to map bars to recording time)")
    sugg = T.suggest(arr, expr, sc, bar_offset, idiomatic, density_bars, set(_list(exclude) or []))
    applied = 0
    if apply:
        if apply not in ("evidence", "safe", "all"):
            raise ArpeggioError("--apply must be evidence, safe or all")
        applied = T.apply(sugg, apply, min_confidence)
        save(arr, output or arrangement)
    rep = arrangement.with_name(arrangement.stem + ".techniques.json")
    pub = T.public(sugg)
    save({"kind": "technique_suggestions", "suggestions": pub}, rep)
    from collections import Counter

    emit({"report": str(rep), "output": str(output or arrangement) if apply else None,
          "suggested": len(pub), "applied": applied,
          "by_technique": dict(Counter(s["technique"] for s in pub)),
          "by_kind": dict(Counter(s["kind"] for s in pub)),
          "suggestions": pub[:30]})


@command
def optimize_tablature(
    arrangement: Path, output: Out = None,
    tuning: Annotated[Optional[str], typer.Option(help="Name (standard, drop_d, dadgad...) or E2,A2,...")] = None,
    capo: Optional[int] = None,
    difficulty: Optional[str] = None,
    repair: Annotated[bool, typer.Option(help="Octave-shift / drop unplayable accompaniment notes")] = True,
):
    """Assign playable string/fret positions to every note."""
    from arpeggio.arrangement.tablature import optimize

    arr, report = optimize(load(arrangement, "arrangement"), tuning, capo, difficulty, repair)
    out = save(arr, output or arrangement)
    report["changes_count"] = len(report["changes"])
    report["changes"] = report["changes"][:40]
    emit({"output": str(out), **report})


@command
def validate_arrangement(
    arrangement: Path,
    max_issues: Annotated[int, typer.Option(help="Issues to print (all are saved)")] = 40,
    severity: Annotated[str, typer.Option(help="Minimum severity printed: error | warning | info")] = "warning",
):
    """Check physical playability; lists issues with suggestions."""
    from arpeggio.arrangement.validate import validate

    res = validate(load(arrangement, "arrangement"))
    report = arrangement.with_name(arrangement.stem + ".validation.json")
    save(res, report)
    order = {"error": 0, "warning": 1, "info": 2}
    shown = [i for i in res["issues"] if order[i["severity"]] <= order[severity]]
    emit({"report": str(report), **{k: v for k, v in res.items() if k != "issues"},
          "issues": shown[:max_issues], "issues_not_shown": max(0, len(shown) - max_issues)})


# ---------------------------------------------------------------------------
# D. Export
# ---------------------------------------------------------------------------


@command
def export(
    arrangement: Path,
    format: Annotated[str, typer.Option(help="gp5 | musicxml | midi | ascii | pdf | all (comma list ok)")] = "all",
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", "-d")] = None,
    name: Annotated[Optional[str], typer.Option(help="Base file name")] = None,
    force: Annotated[bool, typer.Option(help="Export even if validation reports errors")] = False,
    lyrics: Annotated[bool, typer.Option(help="Include the arrangement's lyrics (attach-lyrics) in all formats")] = True,
    gp5_encoding: Annotated[str, typer.Option(help="GP5 text code page: auto | cp1251 | cp1252 | utf-8 …")] = "auto",
    gp5_lyrics: Annotated[str, typer.Option(
        help="guitar: lyrics under the guitar staff (approximate where the melody holds) | "
             "track: extra muted Lyrics track with exact word rhythm")] = "guitar",
):
    """Export a (validated) arrangement to Guitar Pro, MusicXML, MIDI, ASCII tab, PDF."""
    from arpeggio.arrangement.validate import validate
    from arpeggio.config import slugify
    from arpeggio.export.gp5 import export_gp5, read_back
    from arpeggio.export.musicxml import export_musicxml
    from arpeggio.export.simple import export_midi, export_pdf, render_ascii

    arr = load(arrangement, "arrangement")
    v = validate(arr)
    if not v["valid"] and not force:
        raise ArpeggioError(f"arrangement has {v['errors']} playability error(s); run validate-arrangement "
                            "and fix them (or pass --force)")
    fmts = {"gp5", "musicxml", "midi", "ascii", "pdf"} if format == "all" else set(_list(format) or [])
    d = output_dir or arrangement.parent / "export"
    d.mkdir(parents=True, exist_ok=True)
    base = name or slugify(arr.title if arr.title != "Untitled" else arrangement.stem)
    files = {}
    from arpeggio.export.lyrics import gp_encoding

    extra: dict = {}
    gp_texts = [arr.title, arr.artist, arr.arranger] + [x.name for x in arr.sections]
    if lyrics and arr.lyrics:
        from fractions import Fraction

        from arpeggio.export.events import build_events
        from arpeggio.export.lyrics import gp_lyrics_line, place

        num, _, den = arr.time_signature.partition("/")
        evs, ml = build_events(arr)[1], Fraction(int(num) * 4, int(den or 4))
        sl, assign, stats = place(arr, evs, ml)
        gp_texts.append(gp_lyrics_line(sl, assign)[1])
        extra["lyrics"] = {"gp5": stats, "musicxml_pdf": place(arr, evs, ml, voices=(1, 2))[2],
                           "midi": "exact sung onsets (lyric meta events)"}
    elif arr.lyrics:
        extra["lyrics"] = "excluded (--no-lyrics)"
    enc = gp_encoding(gp_texts, gp5_encoding)
    if enc not in ("cp1252", "utf-8"):
        extra["gp5_encoding_note"] = (
            f"GP5 text is {enc} (the format has no Unicode): Guitar Pro / Songsterr show it right on systems "
            f"using that code page; in MuseScore set Preferences → Import → Guitar Pro character set to {enc}. "
            "MusicXML, PDF and MIDI are Unicode.")
    if "gp5" in fmts:
        p = export_gp5(arr, d / f"{base}.gp5", lyrics=lyrics, encoding=enc, lyrics_track=gp5_lyrics == "track")
        files["gp5"] = {"path": str(p), "encoding": enc, "check": read_back(p, enc)}
    if "musicxml" in fmts or "pdf" in fmts:
        files["musicxml"] = str(export_musicxml(arr, d / f"{base}.musicxml", lyrics=lyrics))
    if "midi" in fmts:
        files["midi"] = str(export_midi(arr, d / f"{base}.mid", lyrics=lyrics))
    if "ascii" in fmts:
        p = d / f"{base}.tab.txt"
        p.write_text(render_ascii(arr, lyrics=lyrics), encoding="utf-8")
        files["ascii"] = str(p)
    if "pdf" in fmts:
        files["pdf"] = export_pdf(arr, d / f"{base}.pdf", files.get("musicxml"), lyrics=lyrics)
    emit({"output_dir": str(d), "files": files, **extra,
          "validation": {k: v[k] for k in ("valid", "errors", "warnings")}})


@command
def render_tab(
    arrangement: Path,
    measures: Annotated[Optional[str], typer.Option(help="e.g. 1-8")] = None,
    per_line: int = 4,
    lyrics: Annotated[bool, typer.Option(help="Show attached lyrics under the tab")] = True,
):
    """Print ASCII tablature to stdout (quick look)."""
    from arpeggio.export.simple import render_ascii

    sys.stdout.write(render_ascii(load(arrangement, "arrangement"), per_line, measures=_measures(measures),
                                  lyrics=lyrics))


@command
def export_chord_sheet(
    sheet: Annotated[Path, typer.Argument(help="chord_sheet JSON (align-chords-lyrics)")],
    output: Out = None,
    format: Annotated[str, typer.Option(help="text | chordpro | pdf")] = "text",
    title: Optional[str] = None, artist: Optional[str] = None,
):
    """Render an aligned chord sheet as text, ChordPro or PDF."""
    from arpeggio.analysis.chordsheet import render_chordpro, render_text
    from arpeggio.export.simple import write_text_pdf

    data = load_json(sheet)
    if format == "chordpro":
        text = render_chordpro(data, title, artist)
        out = output or sheet.with_suffix(".cho")
        out.write_text(text, encoding="utf-8")
    elif format == "pdf":
        text = render_text(data, title)
        out = write_text_pdf(text, output or sheet.with_suffix(".pdf"), title=title or "")
    else:
        text = render_text(data, title)
        out = output or sheet.with_suffix(".txt")
        out.write_text(text, encoding="utf-8")
    emit({"output": str(out)})


def main():
    load_env()
    app()


if __name__ == "__main__":
    main()


__all__ = ["app", "main", "json"]
