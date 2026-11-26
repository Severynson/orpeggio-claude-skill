"""Instrument definitions: tunings, ranges and difficulty profiles."""

from __future__ import annotations

from dataclasses import dataclass

from arpeggio.theory import midi_to_name, name_to_midi

# Strings listed from lowest to highest pitch (the way tunings are usually named).
TUNINGS: dict[str, list[str]] = {
    "standard": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "drop_d": ["D2", "A2", "D3", "G3", "B3", "E4"],
    "double_drop_d": ["D2", "A2", "D3", "G3", "B3", "D4"],
    "dadgad": ["D2", "A2", "D3", "G3", "A3", "D4"],
    "open_d": ["D2", "A2", "D3", "F#3", "A3", "D4"],
    "open_g": ["D2", "G2", "D3", "G3", "B3", "D4"],
    "open_c": ["C2", "G2", "C3", "G3", "C4", "E4"],
    "half_step_down": ["Eb2", "Ab2", "Db3", "Gb3", "Bb3", "Eb4"],
    "whole_step_down": ["D2", "G2", "C3", "F3", "A3", "D4"],
    "drop_c": ["C2", "G2", "C3", "F3", "A3", "D4"],
    "bass_standard": ["E1", "A1", "D2", "G2"],
    "bass_5_string": ["B0", "E1", "A1", "D2", "G2"],
}

MIDI_PROGRAMS = {
    "classical_guitar": 24,
    "acoustic_guitar": 25,
    "electric_guitar": 27,
    "bass_guitar": 33,
}


@dataclass(frozen=True)
class Difficulty:
    name: str
    max_span: int  # max fret distance between fretted notes sounding together
    max_fret: int  # highest fret normally used
    max_simultaneous: int  # notes struck at the same instant
    max_fingers: int  # distinct fretted positions held at once (barre counts once)
    shift_speed: float  # frets per second the hand can comfortably travel


DIFFICULTIES = {
    "beginner": Difficulty("beginner", 3, 7, 3, 3, 10.0),
    "intermediate": Difficulty("intermediate", 4, 12, 4, 4, 20.0),
    "advanced": Difficulty("advanced", 5, 17, 6, 4, 40.0),
}


@dataclass
class Fretboard:
    """Concrete instrument: open-string pitches (with capo applied)."""

    tuning_names: list[str]  # low -> high, without capo
    capo: int = 0
    frets: int = 19  # classical guitars have 19, acoustics ~20

    @classmethod
    def from_spec(cls, tuning: list[str] | str, capo: int = 0, instrument: str = "acoustic_guitar"):
        if isinstance(tuning, str):
            key = tuning.lower().replace("-", "_").replace(" ", "_")
            if key in TUNINGS:
                names = TUNINGS[key]
            elif "," in tuning:
                names = [t.strip() for t in tuning.split(",")]
            else:
                raise ValueError(f"unknown tuning '{tuning}'. Known: {', '.join(TUNINGS)}")
        else:
            names = list(tuning)
        if instrument == "bass_guitar" and isinstance(tuning, str) and tuning == "standard":
            names = TUNINGS["bass_standard"]
        frets = {"classical_guitar": 19, "acoustic_guitar": 20, "electric_guitar": 22,
                 "bass_guitar": 20}.get(instrument, 20)
        return cls(names, capo, frets)

    @property
    def open_midi_low_to_high(self) -> list[int]:
        return [name_to_midi(n) + self.capo for n in self.tuning_names]

    @property
    def n_strings(self) -> int:
        return len(self.tuning_names)

    def string_pitch(self, string: int) -> int:
        """Open pitch (with capo) of string number `string` (1 = highest)."""
        return self.open_midi_low_to_high[self.n_strings - string]

    def pitch_at(self, string: int, fret: int) -> int:
        return self.string_pitch(string) + fret

    def positions(self, pitch: int, max_fret: int | None = None) -> list[tuple[int, int]]:
        """All (string, fret) pairs producing `pitch`; frets relative to the capo."""
        limit = min(max_fret if max_fret is not None else self.frets, self.frets - self.capo)
        out = []
        for s in range(1, self.n_strings + 1):
            f = pitch - self.string_pitch(s)
            if 0 <= f <= limit:
                out.append((s, f))
        return out

    @property
    def lowest(self) -> int:
        return min(self.open_midi_low_to_high)

    @property
    def highest(self) -> int:
        return max(self.open_midi_low_to_high) + self.frets - self.capo

    def describe(self) -> dict:
        return {
            "tuning": self.tuning_names,
            "capo": self.capo,
            "strings_high_to_low": [midi_to_name(self.string_pitch(s)) for s in
                                    range(1, self.n_strings + 1)],
            "range": [midi_to_name(self.lowest), midi_to_name(self.highest)],
        }
