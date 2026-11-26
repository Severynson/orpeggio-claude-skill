"""Audio -> instrument-specific note events.

Backends:
* ``muscriptor`` (default): Kyutai/Mirelo multi-instrument transcription model.
  Weights are gated on Hugging Face (CC BY-NC 4.0) — accept the licence and log in.
* ``pyin``: dependency-light fallback. Tracks one monophonic melody line and one
  bass line with librosa's pYIN. Far less accurate, no instrument labels, but
  enough to sketch a melody + bass for an arrangement when the model is unavailable.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import cache_dir, file_sha1, torch_device
from arpeggio.schema import Provenance, Source, TimedNote, Track, Transcription

GM_PROGRAMS = {
    "acoustic_piano": 0, "electric_piano": 4, "chromatic_percussion": 11, "organ": 19,
    "acoustic_guitar": 25, "clean_electric_guitar": 27, "distorted_electric_guitar": 30,
    "acoustic_bass": 32, "electric_bass": 33, "violin": 40, "viola": 41, "cello": 42,
    "contrabass": 43, "orchestral_harp": 46, "timpani": 47, "string_ensemble": 48,
    "synth_strings": 50, "voice": 52, "orchestra_hit": 55, "trumpet": 56, "trombone": 57,
    "tuba": 58, "french_horn": 60, "brass_section": 61, "soprano_and_alto_sax": 65,
    "tenor_sax": 66, "baritone_sax": 67, "oboe": 68, "english_horn": 69, "bassoon": 70,
    "clarinet": 71, "flutes": 73, "synth_lead": 80, "synth_pad": 88, "drums": 0,
    "melody": 73, "bass": 33,
}

MUSCRIPTOR_INSTRUMENTS = [k for k in GM_PROGRAMS if k not in ("melody", "bass")]


def load_audio(path: str | Path, sr: int, start: float | None = None,
               end: float | None = None, mono: bool = True) -> np.ndarray:
    import librosa

    offset = start or 0.0
    duration = (end - offset) if end is not None else None
    y, _ = librosa.load(str(path), sr=sr, mono=mono, offset=offset, duration=duration)
    return y


def audio_duration(path: str | Path) -> float:
    import librosa

    return float(librosa.get_duration(path=str(path)))


def _cache_path(sha1: str, key: dict) -> Path:
    tag = "_".join(f"{k}-{v}" for k, v in sorted(key.items()) if v not in (None, ""))
    tag = tag.replace("/", "-").replace(" ", "")[:120]
    return cache_dir() / "transcriptions" / f"{sha1[:16]}_{tag}.json"


def transcribe_notes(
    audio: str | Path,
    backend: str = "muscriptor",
    model_size: str = "medium",
    instruments: list[str] | None = None,
    start: float | None = None,
    end: float | None = None,
    device: str | None = None,
    use_cache: bool = True,
    constrain: bool = False,
) -> Transcription:
    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    sha1 = file_sha1(audio)
    key = {"b": backend, "m": model_size if backend == "muscriptor" else None,
           "s": start, "e": end,
           "i": "+".join(sorted(instruments)) if constrain and instruments else None}
    cpath = _cache_path(sha1, key)
    if use_cache and cpath.exists():
        log(f"using cached transcription {cpath}")
        tr = Transcription.model_validate(json.loads(cpath.read_text()))
    else:
        if backend == "muscriptor":
            import contextlib
            import sys

            # model libraries print progress on stdout; keep stdout pure JSON
            with contextlib.redirect_stdout(sys.stderr):
                tr = _muscriptor(audio, model_size, start, end, device,
                                 instruments if constrain else None)
        elif backend == "pyin":
            tr = _pyin(audio, start, end)
        else:
            raise ArpeggioError(f"unknown backend '{backend}' (use muscriptor or pyin)")
        tr.source = Source(path=str(audio.resolve()), sha1=sha1,
                           duration_seconds=round(audio_duration(audio), 3))
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(tr.model_dump_json(exclude_none=True))
    if instruments:
        wanted = set(instruments)
        tr.tracks = [t for t in tr.tracks if t.instrument in wanted or t.id in wanted]
        tr.provenance.params["instrument_filter"] = sorted(wanted)
    return tr


# ---------------------------------------------------------------------------
# MuScriptor
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict = {}


def _muscriptor(audio: Path, model_size: str, start, end, device,
                constrain_to: list[str] | None = None) -> Transcription:
    try:
        import torch
        from muscriptor import NoteStartEvent, TranscriptionModel
    except ImportError as e:  # pragma: no cover
        raise ArpeggioError(
            "MuScriptor is not installed. Run `uv sync --extra ml` in the arpeggio repo, "
            "or use --backend pyin.") from e

    dev = torch_device(device)
    log(f"loading MuScriptor '{model_size}' on {dev} (first run downloads weights)")
    try:
        model = _MODEL_CACHE.get((model_size, dev)) or TranscriptionModel.load_model(
            model_size, device=dev)
    except SystemExit as e:  # muscriptor exits on gated download failures
        raise ArpeggioError(_GATED_HINT) from e
    except Exception as e:
        msg = str(e)
        if "gated" in msg.lower() or "401" in msg or "403" in msg or "huggingface" in msg.lower():
            raise ArpeggioError(_GATED_HINT + f" (underlying error: {msg[:300]})") from e
        raise
    _MODEL_CACHE[(model_size, dev)] = model

    sr = 44100
    y = load_audio(audio, sr, start, end, mono=False)
    if y.ndim == 1:
        y = y[None, :]
    wav = torch.from_numpy(np.ascontiguousarray(y, dtype=np.float32))
    offset = start or 0.0

    log("transcribing (≈ real-time or faster on GPU/MPS; slower on CPU)")
    if constrain_to:
        bad = [i for i in constrain_to if i not in MUSCRIPTOR_INSTRUMENTS]
        if bad:
            raise ArpeggioError(f"unknown MuScriptor instrument(s) {bad}; valid: {MUSCRIPTOR_INSTRUMENTS}")
        log(f"constraining decoding to {constrain_to}")
    events = list(model.transcribe((wav, sr), instruments=constrain_to or None))
    starts = [ev for ev in events if isinstance(ev, NoteStartEvent)]
    # Remove the model's constant onset lag using the beat grid, like muscriptor does.
    lag = 0.0
    grid_info = None
    try:
        grid = model.detect_beat_grid_for((wav, sr))
        if grid is not None:
            grid = grid.with_onset_delay([e.start_time for e in starts])
            lag = float(grid.onset_delay or 0.0)
            grid_info = {"bpm": round(float(grid.bpm), 2), "beats_per_bar": grid.beats_per_bar,
                         "first_downbeat": round(float(grid.first_downbeat) + offset, 3),
                         "steady": bool(grid.steady), "onset_delay": round(lag, 4)}
    except Exception as e:  # beat grid is a nicety, never fatal
        log(f"beat grid detection skipped: {e}")

    by_inst: dict[str, list[TimedNote]] = defaultdict(list)
    open_notes = {}
    for ev in events:
        name = type(ev).__name__
        if name == "NoteStartEvent":
            open_notes[ev.index] = ev
        elif name == "NoteEndEvent":
            st = open_notes.pop(ev.start_event_index, ev.start_event)
            s = max(0.0, st.start_time - lag) + offset
            e = max(s + 0.02, ev.end_time - lag + offset)
            by_inst[st.instrument].append(TimedNote(pitch=st.pitch, start=round(s, 4),
                                                    end=round(e, 4)))
    for st in open_notes.values():  # unterminated notes: give them a short tail
        s = max(0.0, st.start_time - lag) + offset
        by_inst[st.instrument].append(TimedNote(pitch=st.pitch, start=round(s, 4),
                                                end=round(s + 0.25, 4)))

    tracks = []
    for inst, notes in sorted(by_inst.items(), key=lambda kv: -len(kv[1])):
        notes.sort(key=lambda n: (n.start, n.pitch))
        tracks.append(Track(id=inst, instrument=inst, program=GM_PROGRAMS.get(inst, 0),
                            is_drum=inst == "drums", notes=notes))
    try:
        from importlib.metadata import version

        ver = version("muscriptor")
    except Exception:
        ver = None
    return Transcription(
        source=Source(path=str(audio)),
        provenance=Provenance(backend="muscriptor", model=f"muscriptor-{model_size}",
                              version=ver, params={"device": dev, "start": start, "end": end,
                                      "constrained_to": constrain_to}),
        tracks=tracks, beat_grid=grid_info)


_GATED_HINT = (
    "Cannot download MuScriptor weights: they are gated on Hugging Face. "
    "1) Accept the CC BY-NC 4.0 licence at https://huggingface.co/MuScriptor/muscriptor-medium "
    "(and -small / -large if used); 2) authenticate with `uvx hf auth login` or set HF_TOKEN. "
    "Alternatively rerun with --backend pyin (melody + bass only, lower accuracy).")


# ---------------------------------------------------------------------------
# pYIN fallback
# ---------------------------------------------------------------------------


def _f0_to_notes(f0: np.ndarray, voiced: np.ndarray, times: np.ndarray, offset: float,
                 min_dur: float = 0.09) -> list[TimedNote]:
    midi = np.where(voiced & np.isfinite(f0), np.round(69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440)),
                    -1).astype(int)
    # median-smooth pitch track to remove single-frame glitches
    from scipy.ndimage import median_filter

    midi = median_filter(midi, size=5)
    notes: list[TimedNote] = []
    cur, t0 = -1, 0.0
    hop = float(times[1] - times[0]) if len(times) > 1 else 0.01
    for i, p in enumerate(list(midi) + [-1]):
        t = times[i] if i < len(times) else times[-1] + hop
        if p != cur:
            if cur > 0 and t - t0 >= min_dur:
                notes.append(TimedNote(pitch=int(cur), start=round(t0 + offset, 4),
                                       end=round(t + offset, 4)))
            cur, t0 = p, t
    return notes


def _pyin(audio: Path, start, end) -> Transcription:
    import librosa

    sr = 22050
    y = load_audio(audio, sr, start, end)
    offset = start or 0.0
    log("pYIN fallback: separating harmonic component")
    yh = librosa.effects.harmonic(y)
    hop = 256
    log("tracking melody line (C3–C6)")
    f0, vflag, _ = librosa.pyin(yh, fmin=librosa.note_to_hz("C3"), fmax=librosa.note_to_hz("C6"),
                                sr=sr, hop_length=hop)
    times = librosa.times_like(f0, sr=sr, hop_length=hop)
    melody = _f0_to_notes(f0, vflag, times, offset)
    log("tracking bass line (E1–E3)")
    from scipy.signal import butter, sosfiltfilt

    sos = butter(6, 220, btype="low", fs=sr, output="sos")
    yb = sosfiltfilt(sos, yh)
    f0b, vb, _ = librosa.pyin(yb, fmin=librosa.note_to_hz("E1"), fmax=librosa.note_to_hz("E3"),
                              sr=sr, hop_length=hop * 2, frame_length=4096)
    timesb = librosa.times_like(f0b, sr=sr, hop_length=hop * 2)
    bass = _f0_to_notes(f0b, vb, timesb, offset, min_dur=0.12)
    return Transcription(
        source=Source(path=str(audio)),
        provenance=Provenance(backend="pyin", model="librosa.pyin",
                              params={"start": start, "end": end,
                                      "warning": "monophonic estimates; verify by ear"}),
        tracks=[Track(id="melody", instrument="melody", program=73, notes=melody),
                Track(id="bass", instrument="bass", program=33, notes=bass)])


# ---------------------------------------------------------------------------
# MIDI writing / summaries
# ---------------------------------------------------------------------------


def write_midi(tr: Transcription, path: str | Path) -> Path:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI()
    for t in tr.tracks:
        inst = pretty_midi.Instrument(program=t.program or 0, is_drum=t.is_drum, name=t.id)
        for n in t.notes:
            inst.notes.append(pretty_midi.Note(velocity=n.velocity, pitch=n.pitch,
                                               start=n.start, end=n.end))
        pm.instruments.append(inst)
    pm.write(str(path))
    return Path(path)


def summarize_transcription(tr: Transcription) -> dict:
    from arpeggio.theory import midi_to_name

    tracks = []
    for t in tr.tracks:
        if not t.notes:
            continue
        pitches = [n.pitch for n in t.notes]
        tracks.append({
            "id": t.id, "notes": len(t.notes),
            "range": None if t.is_drum else [midi_to_name(min(pitches)), midi_to_name(max(pitches))],
            "first": round(t.notes[0].start, 2), "last": round(max(n.end for n in t.notes), 2),
            "max_polyphony": _max_polyphony(t.notes),
        })
    return {"backend": tr.provenance.backend, "model": tr.provenance.model,
            "duration": tr.source.duration_seconds, "tracks": tracks,
            "beat_grid": tr.beat_grid}


def _max_polyphony(notes) -> int:
    ev = sorted([(n.start, 1) for n in notes] + [(n.end, -1) for n in notes])
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best
