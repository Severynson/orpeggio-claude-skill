"""Small, dependency-free music theory helpers shared by every module."""

from __future__ import annotations

import re

NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
_PC = {
    "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "FB": 4,
    "F": 5, "E#": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10,
    "BB": 10, "B": 11, "CB": 11,
}

# Flat-key tonics (major) — used to decide on sharps vs flats when spelling.
_FLAT_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"}


def pitch_class(name: str) -> int:
    key = name.strip().upper().replace("♯", "#").replace("♭", "B")
    if len(key) > 1 and key[1] == "B" and key[0] != "B":
        key = key[0] + "B"
    if key not in _PC:
        raise ValueError(f"unknown pitch class '{name}'")
    return _PC[key]


def midi_to_name(pitch: int, flats: bool = False) -> str:
    names = NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP
    return f"{names[pitch % 12]}{pitch // 12 - 1}"


def name_to_midi(name: str | int) -> int:
    """'E2' -> 40, 'C#4' -> 61, 'Bb3' -> 58. Integers pass through."""
    if isinstance(name, int):
        return name
    s = str(name).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    m = re.fullmatch(r"([A-Ga-g])([#b♯♭]?)(-?\d+)", s)
    if not m:
        raise ValueError(f"cannot parse note name '{name}'")
    pc = pitch_class(m.group(1) + m.group(2))
    return pc + (int(m.group(3)) + 1) * 12


# ---------------------------------------------------------------------------
# Chords
# ---------------------------------------------------------------------------

# Interval sets (semitones above the root) for recognised chord qualities.
CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "5": (0, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "mMaj7": (0, 3, 7, 11),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "7sus4": (0, 5, 7, 10),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "add9": (0, 4, 7, 14),
    "madd9": (0, 3, 7, 14),
}

# lv-chordia / MIREX (Harte) quality -> our suffix.
_HARTE_TO_SUFFIX = {
    "maj": "", "min": "m", "dim": "dim", "aug": "aug", "sus2": "sus2", "sus4": "sus4",
    "7": "7", "maj7": "maj7", "min7": "m7", "minmaj7": "mMaj7", "hdim7": "m7b5",
    "dim7": "dim7", "maj6": "6", "min6": "m6", "9": "9", "maj9": "maj9", "min9": "m9",
    "add9": "add9", "madd9": "madd9", "5": "5", "1": "5", "sus4(b7)": "7sus4",
    "7sus4": "7sus4", "11": "7sus4", "13": "7", "min11": "m7", "maj13": "maj7",
    "min13": "m7",
}

_CHORD_RE = re.compile(
    r"^([A-G][#b]?)"  # root
    r"(maj7|maj9|mMaj7|m7b5|madd9|m6|m7|m9|dim7|dim|aug|sus2|sus4|7sus4|add9|maj|min|m|M7|"
    r"5|6|7|9|11|13|\+|°|ø)?"
    r"(?:\(([^)]*)\))?"
    r"(?:/([A-G][#b]?))?$"
)


def harte_to_symbol(label: str) -> str:
    """'A:min' -> 'Am', 'C:maj/3' -> 'C/E', 'N' -> 'N'."""
    if label in ("N", "X", ""):
        return "N"
    root, _, rest = label.partition(":")
    quality, _, bass = (rest or "maj").partition("/")
    quality = quality.split("(")[0] if quality not in _HARTE_TO_SUFFIX else quality
    suffix = _HARTE_TO_SUFFIX.get(quality, "")
    symbol = root + suffix
    if bass:
        try:
            interval = _degree_to_semitones(bass)
            bass_pc = (pitch_class(root) + interval) % 12
            flats = "b" in root
            symbol += "/" + (NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP)[bass_pc]
        except ValueError:
            pass
    return symbol


def _degree_to_semitones(degree: str) -> int:
    base = {"1": 0, "2": 2, "3": 4, "4": 5, "5": 7, "6": 9, "7": 11, "9": 14, "11": 17, "13": 21}
    acc = 0
    d = degree
    while d and d[0] in "b#":
        acc += -1 if d[0] == "b" else 1
        d = d[1:]
    if d not in base:
        raise ValueError(degree)
    return (base[d] + acc) % 12


def parse_chord(symbol: str) -> dict:
    """Parse a chord symbol into root/quality/bass and pitch classes.

    Returns {"root": "A", "quality": "m", "bass": None, "pitch_classes": [...]}.
    Raises ValueError for unrecognised symbols.
    """
    s = symbol.strip().replace("♯", "#").replace("♭", "b")
    m = _CHORD_RE.match(s)
    if not m:
        raise ValueError(f"unrecognised chord symbol '{symbol}'")
    root, quality, _ext, bass = m.groups()
    quality = {
        None: "", "maj": "", "min": "m", "M7": "maj7", "+": "aug", "°": "dim",
        "ø": "m7b5", "11": "7sus4", "13": "7",
    }.get(quality, quality)
    intervals = CHORD_QUALITIES.get(quality, CHORD_QUALITIES[""])
    rpc = pitch_class(root)
    pcs = sorted({(rpc + i) % 12 for i in intervals})
    out = {"root": root, "quality": quality, "bass": bass, "pitch_classes": pcs,
           "root_pc": rpc}
    if bass:
        out["bass_pc"] = pitch_class(bass)
    return out


def is_chord_symbol(token: str) -> bool:
    if token in ("N", "N.C.", "NC"):
        return True
    try:
        parse_chord(token)
        return True
    except ValueError:
        return False


def transpose_chord(symbol: str, semitones: int, flats: bool | None = None) -> str:
    if symbol in ("N", "N.C.", "NC"):
        return symbol
    c = parse_chord(symbol)
    if flats is None:
        flats = "b" in c["root"]
    names = NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP
    out = names[(c["root_pc"] + semitones) % 12] + c["quality"]
    if c["bass"]:
        out += "/" + names[(c["bass_pc"] + semitones) % 12]
    return out


# ---------------------------------------------------------------------------
# Keys (Krumhansl-Kessler profiles)
# ---------------------------------------------------------------------------

KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate_key(pc_weights: list[float]) -> list[dict]:
    """Rank the 24 major/minor keys by correlation with a pitch-class histogram."""
    import numpy as np

    w = np.asarray(pc_weights, dtype=float)
    if w.sum() <= 0:
        return []
    results = []
    for mode, prof in (("major", KK_MAJOR), ("minor", KK_MINOR)):
        p = np.asarray(prof)
        for tonic in range(12):
            r = np.corrcoef(w, np.roll(p, tonic))[0, 1]
            name = key_name(tonic, mode)
            results.append({"key": name, "tonic": name.split()[0], "mode": mode,
                            "correlation": round(float(r), 3)})
    results.sort(key=lambda d: -d["correlation"])
    return results


def key_name(tonic_pc: int, mode: str) -> str:
    sharp = NOTE_NAMES_SHARP[tonic_pc]
    flat = NOTE_NAMES_FLAT[tonic_pc]
    if mode == "major":
        tonic = flat if flat in _FLAT_KEYS else sharp
        if tonic_pc == 6:
            tonic = "F#"
    else:
        rel_major = NOTE_NAMES_FLAT[(tonic_pc + 3) % 12]
        tonic = flat if rel_major in _FLAT_KEYS else sharp
    return f"{tonic} {mode}"


def prefers_flats(key: str | None) -> bool:
    if not key:
        return False
    tonic, _, mode = key.partition(" ")
    if mode.startswith("min"):
        rel = NOTE_NAMES_FLAT[(pitch_class(tonic) + 3) % 12]
        return rel in _FLAT_KEYS
    return tonic in _FLAT_KEYS


# Fifths on the circle (for GP5 / MusicXML key signatures). Major tonic pc -> fifths.
_MAJOR_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 5: -1, 10: -2, 3: -3, 8: -4, 1: -5}


def key_fifths(key: str | None) -> tuple[int, bool]:
    """Return (fifths, is_minor) for a key like 'A minor'. Defaults to C major."""
    if not key:
        return 0, False
    tonic, _, mode = key.partition(" ")
    pc = pitch_class(tonic)
    minor = mode.lower().startswith("min")
    major_pc = (pc + 3) % 12 if minor else pc
    fifths = _MAJOR_FIFTHS[major_pc]
    if fifths == -5 and "#" in tonic:
        fifths = 7
    if fifths == 6 and "b" in tonic:
        fifths = -6
    return fifths, minor
