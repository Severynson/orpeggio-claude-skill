"""Put timed lyrics (seconds in the recording) onto an arrangement's bars and beats."""

from __future__ import annotations

from bisect import bisect_right

from arpeggio.arrangement.tablature import measure_quarters
from arpeggio.schema import Arrangement, ArrLyric, Lyrics, Score


def attach_lyrics(arr: Arrangement, lyrics: Lyrics, score: Score, bar_offset: int = 0) -> tuple[Arrangement, dict]:
    """Map every word's onset to (measure, beat) with the score's measure_starts
    (tempo drift safe). Arrangement bar = recording bar - bar_offset. Replaces
    any lyrics the arrangement had."""
    starts = list(score.measure_starts)
    if len(starts) < 2:
        raise ValueError("score has no measure_starts (run quantize on a beat-tracked transcription)")
    mlen = measure_quarters(arr.time_signature)
    typical = (starts[-1] - starts[0]) / (len(starts) - 1)
    last_m = arr.measure_count or max([n.measure for n in arr.notes] + [1])
    out = arr.model_copy(deep=True)
    out.lyrics = []
    dropped = []
    for li, ln in enumerate(lyrics.lines):
        for w in ln.words:
            i = bisect_right(starts, w.start) - 1
            if i < 0:
                dropped.append(w.text)
                continue
            bar_len = (starts[i + 1] - starts[i]) if i + 1 < len(starts) else typical
            beat = (w.start - starts[i]) / bar_len * mlen
            m = (i + 1) - bar_offset
            if beat >= mlen:  # past the last known downbeat
                m += int(beat // mlen)
                beat %= mlen
            if not 1 <= m <= last_m:
                dropped.append(w.text)
                continue
            out.lyrics.append(ArrLyric(text=w.text, measure=m, beat=round(beat, 3), line=li, source=w.source))
    out.lyrics.sort(key=lambda x: (x.measure, x.beat))
    bars = sorted({x.measure for x in out.lyrics})
    return out, {"words": len(out.lyrics), "dropped": dropped, "lines": len(lyrics.lines),
                 "first_measure": bars[0] if bars else None, "last_measure": bars[-1] if bars else None}
