"""Lyrics transcription and forced alignment via the ElevenLabs API.

* transcribe: Scribe speech-to-text with word timestamps.
* align: forced alignment of user-supplied lyrics to the audio.

Optionally isolates vocals with Demucs first (``--isolate-vocals``), which
usually improves accuracy on dense mixes.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import file_sha1, load_env
from arpeggio.schema import LyricLine, Lyrics, Provenance, Source, Word

API = "https://api.elevenlabs.io/v1"


def _key() -> str:
    load_env()
    key = os.environ.get("ELEVEN_LABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ArpeggioError("ElevenLabs API key missing: set ELEVENLABS_API_KEY "
                            "(or ELEVEN_LABS_API_KEY) in the environment or .env")
    return key


def _upload_copy(audio: Path, start: float | None, end: float | None) -> tuple[Path, bool]:
    """Return a (possibly trimmed, compressed) file to upload, and whether it is temporary."""
    if start is None and end is None and audio.stat().st_size < 900 * 1024 * 1024:
        return audio, False
    import subprocess

    tmp = Path(tempfile.mkstemp(suffix=".mp3")[1])
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", str(audio), "-ac", "1", "-b:a", "128k", str(tmp)]
    subprocess.run(cmd, check=True)
    return tmp, True


def _post(endpoint: str, audio: Path, data: dict, timeout: float = 600) -> dict:
    import httpx

    with open(audio, "rb") as f:
        r = httpx.post(f"{API}/{endpoint}", headers={"xi-api-key": _key()},
                       data=data, files={"file": (audio.name, f)}, timeout=timeout)
    if r.status_code >= 400:
        msg = f"ElevenLabs {endpoint} failed ({r.status_code}): {r.text[:400]}"
        if "missing_permissions" in r.text:
            perm = "speech_to_text" if endpoint == "speech-to-text" else "forced_alignment / speech_to_text"
            msg += (f" — enable the '{perm}' permission for this API key at "
                    "https://elevenlabs.io/app/settings/api-keys (or create a key with it).")
        raise ArpeggioError(msg)
    return r.json()


def _maybe_isolate(audio: Path, isolate: bool, device: str | None) -> Path:
    if not isolate:
        return audio
    from arpeggio.transcription.separation import separate

    stems = separate(audio, stems="vocals", device=device)
    return Path(stems["vocals"])


def transcribe_lyrics(audio: str | Path, language: str | None = None,
                      isolate_vocals: bool = False, model: str = "scribe_v2",
                      start: float | None = None, end: float | None = None,
                      device: str | None = None) -> Lyrics:
    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    src = _maybe_isolate(audio, isolate_vocals, device)
    up, tmp = _upload_copy(src, start, end)
    data = {"model_id": model, "timestamps_granularity": "word", "tag_audio_events": "false",
            "diarize": "false"}
    if language:
        data["language_code"] = language
    log(f"ElevenLabs speech-to-text ({model})")
    try:
        try:
            res = _post("speech-to-text", up, data)
        except ArpeggioError as e:
            if model == "scribe_v2" and ("model" in str(e).lower()):
                data["model_id"] = model = "scribe_v1"
                res = _post("speech-to-text", up, data)
            else:
                raise
    finally:
        if tmp:
            up.unlink(missing_ok=True)
    offset = start or 0.0
    words = [Word(text=w["text"].strip(), start=round(w["start"] + offset, 3),
                  end=round(w["end"] + offset, 3), speaker=w.get("speaker_id"))
             for w in res.get("words", []) if w.get("type", "word") == "word" and w["text"].strip()]
    return Lyrics(source=Source(path=str(audio.resolve()), sha1=file_sha1(audio)),
                  provenance=Provenance(backend="elevenlabs", model=model,
                                        params={"isolate_vocals": isolate_vocals}),
                  language=res.get("language_code"), text=res.get("text", "").strip(),
                  lines=_group_lines(words))


def align_lyrics(audio: str | Path, lyrics_text: str, isolate_vocals: bool = False,
                 start: float | None = None, end: float | None = None,
                 device: str | None = None) -> Lyrics:
    """Forced alignment: keep the user's words and line breaks, add timestamps."""
    from arpeggio.analysis.lyrics_merge import clean_lyrics_text, norm

    audio = Path(audio)
    lines_in = [ln for _, ln in clean_lyrics_text(lyrics_text) if not _is_section_label(ln)]
    if not lines_in:
        raise ArpeggioError("no lyric lines to align")
    src = _maybe_isolate(audio, isolate_vocals, device)
    up, tmp = _upload_copy(src, start, end)
    log("ElevenLabs forced alignment")
    try:
        res = _post("forced-alignment", up, {"text": "\n".join(lines_in)})
    finally:
        if tmp:
            up.unlink(missing_ok=True)
    offset = start or 0.0
    aligned = [Word(text=w["text"].strip(), start=round(w["start"] + offset, 3),
                    end=round(w["end"] + offset, 3),
                    confidence=round(1.0 - float(w.get("loss", 0.0)), 3) if "loss" in w else None)
               for w in res.get("words", []) if w["text"].strip()]
    lines: list[LyricLine] = []
    i = 0
    for ln in lines_in:
        n = len(ln.split())
        ws = [w for w in aligned[i:i + n] if norm(w.text)]  # drop sung-less tokens such as "—"
        i += n
        if ws:
            lines.append(LyricLine(text=ln, start=ws[0].start, end=ws[-1].end, words=ws))
    return Lyrics(source=Source(path=str(audio.resolve()), sha1=file_sha1(audio)),
                  provenance=Provenance(backend="elevenlabs", model="forced-alignment",
                                        params={"isolate_vocals": isolate_vocals}),
                  text="\n".join(lines_in), lines=lines)


def _is_section_label(line: str) -> bool:
    return bool(re.fullmatch(r"[\[(]?[A-Za-z\- ]+\d*[\])]?:?", line)) and line.lower().strip("[]():").split()[0] in {
        "verse", "chorus", "bridge", "intro", "outro", "pre-chorus", "prechorus", "refrain", "solo",
        "interlude", "hook", "coda"}


def _group_lines(words: list[Word], gap: float = 0.65, max_words: int = 12) -> list[LyricLine]:
    lines: list[list[Word]] = []
    for w in words:
        if not lines:
            lines.append([w])
            continue
        prev = lines[-1][-1]
        pause = w.start - prev.end
        punct = prev.text[-1:] in ".!?;"
        if pause > gap or (punct and pause > 0.25) or (len(lines[-1]) >= max_words and pause > 0.2):
            lines.append([w])
        else:
            lines[-1].append(w)
    return [LyricLine(text=" ".join(x.text for x in ws), start=ws[0].start, end=ws[-1].end, words=ws)
            for ws in lines]
