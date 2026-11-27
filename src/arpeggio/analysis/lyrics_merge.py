"""Merge user-supplied lyrics text with a timestamped speech-to-text transcript.

The user's text is the reliable source of *words*; the transcript (ElevenLabs
Scribe) is the source of *timing* and usually has wrong words in sung/shouted
material. We align the two word sequences with an edit-distance DP whose
substitution cost is the spelling distance of the two words:

* match / substitution  -> the user's word takes the transcript word's timestamps
  (a substitution is exactly "replace the misheard word by the right one");
* user word with no partner -> timestamps interpolated between its neighbours;
* transcript words with no partner -> reported (sung words missing from the text,
  e.g. an extra chorus repeat), never added to the lyrics.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from arpeggio.schema import LyricLine, Lyrics, Provenance, Word

_SECTION = re.compile(r"^\s*[\[(][^\])]{1,40}[\])]\s*$")
_GENIUS_JUNK = re.compile(r"you might also like|^\d*\s*embed$|^\d+\s+contributors?", re.I)
_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", re.U)


def clean_lyrics_text(text: str) -> list[tuple[str | None, str]]:
    """Lyrics text -> [(section, line)]. Drops [Section] headers (kept as labels),
    blank lines and lyrics-site junk ("You might also like" blocks, "Embed")."""
    out: list[tuple[str | None, str]] = []
    section = None
    skipping = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SECTION.match(line):
            section, skipping = line.strip("[]() "), False
            continue
        m = re.search(r"you might also like", line, re.I)
        if m:
            head = line[:m.start()].strip()
            if head:
                out.append((section, head))
            skipping = True  # recommendation list follows until the next [Section]
            continue
        if skipping or _GENIUS_JUNK.search(line):
            continue
        line = re.sub(r"\d*\s*Embed$", "", line).strip()
        if line:
            out.append((section, line))
    return out


def norm(word: str) -> str:
    w = _PUNCT.sub("", word.lower()).replace("ё", "е")
    return w


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _align(user: list[str], stt: list[str], gap_user: float = 1.0, gap_stt: float = 0.55):
    # gap_user is dearer: every user word is sung, and the transcript usually emits
    # *something* for it (often misheard); extra transcript words are more common
    """Edit-distance alignment. Returns pairs (i_user | None, j_stt | None, sim)."""
    n, m = len(user), len(stt)
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c = cost[i][j]
            if c == INF:
                continue
            if i < n and j < m:
                s = _sim(user[i], stt[j])
                good = 0.5 if min(len(user[i]), len(stt[j])) >= 5 else 0.7  # short words look alike
                sub = 1.0 - s if s >= good else 1.0 + 0.2 * (1.0 - s)  # poor spelling match: still
                sub += min(0.4, 0.08 * abs(_syllables(user[i]) - _syllables(stt[j])))  # cheaper than 2 gaps
                if c + sub < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c + sub, (i, j, "sub", s)
            if i < n and c + gap_user < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = c + gap_user, (i, j, "user", 0.0)
            if j < m and c + gap_stt < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = c + gap_stt, (i, j, "stt", 0.0)
    pairs = []
    i, j = n, m
    while (i, j) != (0, 0):
        pi, pj, kind, s = back[i][j]
        pairs.append((pi if kind in ("sub", "user") else None, pj if kind in ("sub", "stt") else None, s))
        i, j = pi, pj
    return pairs[::-1]


def _syllables(word: str) -> int:
    return max(1, len(re.findall(r"[aeiouyаеёиоуыэюяіїєAEIOUYАЕЁИОУЫЭЮЯІЇЄ]", word)))


def merge_lyrics(stt: Lyrics, text: str, sec_per_syllable: float = 0.22) -> tuple[Lyrics, dict]:
    lines_in = clean_lyrics_text(text)
    if not lines_in:
        raise ValueError("no lyric lines in the text")
    user_words: list[tuple[int, str]] = []  # (line index, word)
    for li, (_, line) in enumerate(lines_in):
        for w in line.split():
            if norm(w):  # drop standalone punctuation such as "—"
                user_words.append((li, w))
    stt_words = [w for ln in stt.lines for w in ln.words]
    pairs = _align([norm(w) for _, w in user_words], [norm(w.text) for w in stt_words])

    timing: list[tuple[float, float, str, float] | None] = [None] * len(user_words)
    extra: list[list[Word]] = []
    run: list[Word] = []
    for iu, js, s in pairs:
        if iu is not None and js is not None:
            w = stt_words[js]
            timing[iu] = (w.start, w.end, "matched" if s >= 0.75 else "corrected", s)
        if iu is None and js is not None:
            run.append(stt_words[js])
        elif run:
            extra.append(run)
            run = []
    if run:
        extra.append(run)

    # interpolate words without a transcript partner
    n = len(user_words)
    i = 0
    while i < n:
        if timing[i] is not None:
            i += 1
            continue
        k = i
        while k < n and timing[k] is None:
            k += 1
        prev = timing[i - 1] if i > 0 else None
        nxt = timing[k] if k < n else None
        sylls = [_syllables(user_words[x][1]) for x in range(i, k)]
        need = sum(sylls) * sec_per_syllable
        if prev and nxt:
            span = nxt[0] - prev[1]
            same_prev = user_words[i - 1][0] == user_words[i][0]
            same_next = user_words[k][0] == user_words[k - 1][0]
            if span <= 1.5 * need or (same_prev and same_next):
                t0, scale = prev[1], min(1.0, span / need) if need else 1.0
                t0 += max(0.0, (span - need * scale) / 2) if not (same_prev or same_next) else 0.0
            elif same_next and not same_prev:
                t0, scale = nxt[0] - need, 1.0  # belongs to the next line's start
            else:
                t0, scale = prev[1] + 0.1, 1.0  # continues the previous line
        elif prev:
            t0, scale = prev[1] + 0.1, 1.0
        elif nxt:
            t0, scale = max(0.0, nxt[0] - need), 1.0
        else:
            t0, scale = 0.0, 1.0
        t = t0
        for x, sy in zip(range(i, k), sylls):
            d = sy * sec_per_syllable * scale
            timing[x] = (round(t, 3), round(t + d, 3), "interpolated", 0.0)
            t += d
        i = k

    out_lines: list[LyricLine] = []
    by_line: dict[int, list[Word]] = {}
    for (li, w), (st, en, how, s) in zip(user_words, timing):
        by_line.setdefault(li, []).append(
            Word(text=w, start=st, end=en, confidence=round(s, 2) if how != "interpolated" else None,
                 source=how))
    for li, (_, line) in enumerate(lines_in):
        ws = by_line.get(li)
        if ws:
            out_lines.append(LyricLine(text=line, start=ws[0].start, end=ws[-1].end, words=ws))
    merged = Lyrics(source=stt.source,
                    provenance=Provenance(backend="merge", model=stt.provenance.model,
                                          params={"text": "user", "timing": stt.provenance.backend}),
                    language=stt.language, text="\n".join(l for _, l in lines_in), lines=out_lines)
    counts = {"matched": 0, "corrected": 0, "interpolated": 0}
    for t in timing:
        counts[t[2]] += 1
    corrections = [{"word": w, "heard": stt_words[js].text, "time": stt_words[js].start}
                   for (iu, js, s) in pairs if iu is not None and js is not None
                   for w in [user_words[iu][1]] if s < 0.75]
    report = {"user_words": n, "transcript_words": len(stt_words), **counts,
              "corrections": corrections[:60],
              "sections": sorted({s for s, _ in lines_in if s}),
              "unmatched_transcript": [{"start": r[0].start, "end": r[-1].end,
                                        "text": " ".join(w.text for w in r)} for r in extra if len(r) >= 1]}
    return merged, report


# ---------------------------------------------------------------------------
# Re-timing with vocal activity (fixes transcript/alignment timing collapses)
# ---------------------------------------------------------------------------

def vocal_segments(wav_path, top_db: float = 18.0, min_len: float = 0.4, hop_s: float = 0.1):
    """Voice-active segments [(start, end)] of an isolated vocal stem."""
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(wav_path), always_2d=True)
    y = y.mean(axis=1)
    hop = max(1, int(sr * hop_s))
    frames = len(y) // hop
    rms = np.sqrt(np.mean(y[:frames * hop].reshape(frames, hop) ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-9)
    active = db > np.percentile(db, 95) - top_db
    segs, start = [], None
    for i, a in enumerate(list(active) + [False]):
        if a and start is None:
            start = i
        elif not a and start is not None:
            if (i - start) * hop_s >= min_len:
                segs.append([start * hop_s, i * hop_s])
            start = None
    merged: list[list[float]] = []
    for a, b in segs:  # bridge breaths between syllables
        if merged and a - merged[-1][1] < 0.35:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in merged]


def _line_ok(ln: LyricLine, segs, spsyl: float) -> bool:
    if all(w.source == "interpolated" for w in ln.words):
        return False  # no anchor in the transcript at all
    for k, w in enumerate(ln.words):
        need = _syllables(w.text) * spsyl
        if w.end - w.start > max(2.0, 3 * need + 0.5):
            return False  # a word cannot last that long: the timing collapsed
        if k + 1 < len(ln.words) and ln.words[k + 1].start - w.end > 2.5:
            return False  # a sung line is not torn apart by seconds of silence
        if segs and not any(a - 1.0 <= w.start <= b + 1.0 for a, b in segs):
            return False  # no voice anywhere near the word
    return True


def retime_with_vocals(lyrics: Lyrics, segs, sec_per_syllable: float = 0.22) -> tuple[Lyrics, list[dict]]:
    """Move lines with impossible timing into unclaimed voice-active segments,
    in order, between their reliable neighbours. Returns (lyrics, changes)."""
    if not segs:
        return lyrics, []
    out = lyrics.model_copy(deep=True)
    lines = out.lines
    ok = [_line_ok(ln, segs, sec_per_syllable) for ln in lines]
    claimed = set()
    for ln, good in zip(lines, ok):
        if good:
            for w in ln.words:
                for si, (a, b) in enumerate(segs):
                    if a - 0.2 <= w.start <= b + 0.2:
                        claimed.add(si)
    changes = []
    i = 0
    while i < len(lines):
        if ok[i]:
            i += 1
            continue
        k = i
        while k < len(lines) and not ok[k]:
            k += 1
        lo = lines[i - 1].end if i > 0 else 0.0
        hi = lines[k].start if k < len(lines) else float("inf")
        free = [si for si, (a, b) in enumerate(segs) if si not in claimed and a >= lo - 0.1 and b <= hi + 0.1]
        for ln in lines[i:k]:
            need = sum(_syllables(w.text) for w in ln.words) * sec_per_syllable
            pick = next((si for si in free if segs[si][1] - segs[si][0] >= 0.5 * need), None)
            if pick is None:
                continue
            free = [si for si in free if si > pick]
            claimed.add(pick)
            a, b = segs[pick]
            scale = min(1.0, (b - a) / need) if need else 1.0
            old = ln.start
            t = old if a <= old <= b - 0.5 * need * scale else a  # keep a start that already fits
            scale = min(scale, (b - t) / need) if need else scale
            for w in ln.words:
                d = _syllables(w.text) * sec_per_syllable * scale
                w.start, w.end, w.source = round(t, 3), round(t + d, 3), "retimed"
                t += d
            ln.start, ln.end = ln.words[0].start, ln.words[-1].end
            changes.append({"line": ln.text, "from": old, "to": ln.start})
        i = k
    return out, changes


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def combine_lines(primary: Lyrics, secondary: Lyrics | None, segs, spsyl: float = 0.22) -> tuple[Lyrics, list]:
    """Per line, keep the primary timing (forced alignment of the user's text) when it
    is plausible, else the secondary (transcript merge) when that one is."""
    if secondary is None or len(secondary.lines) != len(primary.lines):
        return primary, []
    out = primary.model_copy(deep=True)
    used = []
    for k, (a, b) in enumerate(zip(out.lines, secondary.lines)):
        anchored = sum(w.source == "matched" for w in b.words) >= max(1, len(b.words) / 2)
        if not _line_ok(a, segs, spsyl) and _line_ok(b, segs, spsyl) and anchored:
            out.lines[k] = b.model_copy(deep=True)
            used.append(b.text)
    return out, used


def sync_lyrics(audio, text: str | None = None, transcript: Lyrics | None = None,
                language: str | None = None, isolate_vocals: bool = True,
                method: str = "auto", device: str | None = None) -> tuple[Lyrics, dict]:
    """Best-effort timed lyrics.

    * no `text`: ElevenLabs transcript (words may be wrong);
    * with `text` (the user's lyrics are the trusted words): forced alignment of
      the text + transcript merge as a second opinion (method auto), or only one
      of them (method forced | transcript);
    * finally lines with collapsed timing are moved into unclaimed voice-active
      segments of the vocal stem (when Demucs is available).
    """
    from pathlib import Path

    from arpeggio.artifacts import log
    from arpeggio.transcription import lyrics as L

    audio = Path(audio)
    report: dict = {"method": method, "text": "user" if text else "transcript"}
    vocals = None
    if isolate_vocals:
        try:
            vocals = L._maybe_isolate(audio, True, device)
        except Exception as e:  # separation extra missing: continue on the full mix
            log(f"vocal isolation unavailable ({e}); using the full mix")
            report["vocal_isolation"] = f"unavailable: {e}"
    segs = vocal_segments(vocals) if vocals else []
    report["voice_segments"] = len(segs)

    need_transcript = text is None or method in ("auto", "transcript")
    if transcript is None and need_transcript:
        transcript = L.transcribe_lyrics(audio, language=language, isolate_vocals=bool(vocals), device=device)
    if text is None:
        result = transcript
    else:
        merged = None
        if transcript is not None and method in ("auto", "transcript"):
            merged, mrep = merge_lyrics(transcript, text)
            report["transcript_merge"] = {k: mrep[k] for k in ("matched", "corrected", "interpolated")}
            report["corrections"] = mrep["corrections"]
            report["unmatched_transcript"] = mrep["unmatched_transcript"]
        if method in ("auto", "forced"):
            forced = L.align_lyrics(audio, text, isolate_vocals=bool(vocals), device=device)
            for ln in forced.lines:
                for w in ln.words:
                    w.source = "aligned"
            result, used = combine_lines(forced, merged, segs)
            report["lines_from_transcript"] = used
        else:
            result = merged
    result, changes = retime_with_vocals(result, segs)
    report["retimed_lines"] = changes
    result.provenance.params.update({"method": method, "user_text": bool(text)})
    return result, report
