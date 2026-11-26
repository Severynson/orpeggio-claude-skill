"""Shared symbolic music representation.

Two time systems are used and kept consistent everywhere:

* **seconds** (`start`, `end`) — measured from the beginning of the original
  recording. Every perception command (notes, chords, lyrics, rhythm) uses them,
  so their outputs can be combined directly.
* **musical time** (`measure`, `beat`, `duration`) — after quantization.
  `measure` is 1-based; `beat` is the offset inside the measure in quarter notes
  (0 = downbeat); `duration` is in quarter notes.

Every artifact is a JSON file with a `kind` field naming its type.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Provenance(_Model):
    backend: str
    model: str | None = None
    version: str | None = None
    params: dict = Field(default_factory=dict)


class Source(_Model):
    path: str
    sha1: str | None = None
    duration_seconds: float | None = None


# ---------------------------------------------------------------------------
# Perception outputs (seconds)
# ---------------------------------------------------------------------------


class TimedNote(_Model):
    pitch: int
    start: float
    end: float
    velocity: int = 80
    confidence: float | None = None


class Track(_Model):
    id: str
    instrument: str
    program: int | None = None
    is_drum: bool = False
    notes: list[TimedNote] = Field(default_factory=list)


class Transcription(_Model):
    kind: Literal["transcription"] = "transcription"
    source: Source
    provenance: Provenance
    tracks: list[Track] = Field(default_factory=list)
    beat_grid: dict | None = None
    midi_path: str | None = None


class Rhythm(_Model):
    kind: Literal["rhythm"] = "rhythm"
    source: Source
    provenance: Provenance
    tempo_bpm: float
    beats_per_bar: int
    time_signature: str
    steady_tempo: bool = True
    beats: list[float]
    downbeats: list[float]
    tempo_curve: list[dict] = Field(default_factory=list)


class ChordSegment(_Model):
    chord: str
    start: float
    end: float
    raw_label: str | None = None
    confidence: float | None = None


class Chords(_Model):
    kind: Literal["chords"] = "chords"
    source: Source
    provenance: Provenance
    chords: list[ChordSegment]


class Word(_Model):
    text: str
    start: float
    end: float
    confidence: float | None = None
    speaker: str | None = None
    source: str | None = None  # merge-lyrics: matched | corrected | interpolated


class LyricLine(_Model):
    text: str
    start: float
    end: float
    words: list[Word]


class Lyrics(_Model):
    kind: Literal["lyrics"] = "lyrics"
    source: Source
    provenance: Provenance
    language: str | None = None
    text: str = ""
    lines: list[LyricLine]


# ---------------------------------------------------------------------------
# Musical time
# ---------------------------------------------------------------------------


class QNote(_Model):
    pitch: int
    name: str | None = None
    measure: int
    beat: float
    duration: float
    start: float | None = None
    end: float | None = None
    velocity: int = 80


class QTrack(_Model):
    id: str
    instrument: str
    is_drum: bool = False
    notes: list[QNote] = Field(default_factory=list)


class QChord(_Model):
    chord: str
    measure: int
    beat: float
    duration: float
    start: float | None = None


class Score(_Model):
    """Quantized multi-track score (output of `quantize`)."""

    kind: Literal["score"] = "score"
    source: Source | None = None
    tempo_bpm: float
    time_signature: str = "4/4"
    beats_per_bar: int = 4
    grid: str = "1/16"
    measure_count: int
    measure_starts: list[float] = Field(default_factory=list)
    key: str | None = None
    tracks: list[QTrack] = Field(default_factory=list)
    chords: list[QChord] = Field(default_factory=list)
    lyrics: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Arrangement (written by the agent, enriched by optimize-tablature)
# ---------------------------------------------------------------------------

Role = Literal["melody", "bass", "harmony", "counter", "percussive"]


class ArrNote(_Model):
    pitch: int | str
    measure: int
    beat: float
    duration: float
    role: Role = "melody"
    voice: int | None = None  # 1 = stems up (melody), 2 = stems down (bass)
    string: int | None = None  # 1 = highest-pitched string
    fret: int | None = None
    locked: bool = False  # optimizer must keep string/fret
    finger: str | None = None  # p i m a (right hand) — optional
    technique: str | None = None  # "h", "p", "slide", "harmonic", "let_ring", ...
    tie: bool = False  # continuation of the previous note on this string


class ArrChord(_Model):
    chord: str
    measure: int
    beat: float = 0.0


class ArrLyric(_Model):
    text: str  # one sung word (or syllable)
    measure: int
    beat: float  # quarter notes from the start of the measure (sung onset)
    line: int = 0  # lyric line index (for line breaks)
    source: str | None = None  # aligned | matched | corrected | interpolated | retimed | manual


class Section(_Model):
    name: str
    start_measure: int
    end_measure: int


class Arrangement(_Model):
    kind: Literal["arrangement"] = "arrangement"
    title: str = "Untitled"
    artist: str = ""
    arranger: str = "Arpeggio + Claude"
    instrument: Literal["acoustic_guitar", "classical_guitar", "electric_guitar",
                        "bass_guitar"] = "acoustic_guitar"
    tuning: list[str] | str = "standard"
    capo: int = 0
    difficulty: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    tempo_bpm: float = 100
    time_signature: str = "4/4"
    key: str | None = None
    measure_count: int | None = None
    sections: list[Section] = Field(default_factory=list)
    chords: list[ArrChord] = Field(default_factory=list)
    notes: list[ArrNote] = Field(default_factory=list)
    lyrics: list[ArrLyric] = Field(default_factory=list)  # sung words on the timeline (attach-lyrics)
    notes_text: str | None = None  # free-form arranger remarks


KINDS = {
    "transcription": Transcription,
    "rhythm": Rhythm,
    "chords": Chords,
    "lyrics": Lyrics,
    "score": Score,
    "arrangement": Arrangement,
}
