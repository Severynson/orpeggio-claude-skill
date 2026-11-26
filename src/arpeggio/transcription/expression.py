"""Detect expressive pitch gestures (vibrato, bends) of known notes in audio.

Because the pitch and timing of every note are already known from the
transcription, we do not need a monophonic pitch tracker: for each note we
follow the spectral peak of its fundamental (or a stronger low harmonic) in a
narrow band of ±80 cents around the expected frequency. That works inside
dense mixes, where general-purpose f0 trackers fail.

* vibrato: periodic deviation, rate 4–9 Hz, peak depth >= ~15 cents, >= 2 cycles
* bend:    sustained rise (or fall) of >= ~70 cents relative to the note's start
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from arpeggio.artifacts import ArpeggioError, log
from arpeggio.config import file_sha1
from arpeggio.schema import Transcription

SR = 22050
N_FFT = 4096
HOP = 128


def _midi_hz(p: float) -> float:
    return 440.0 * 2 ** ((p - 69) / 12)


def _track_note(S: np.ndarray, freqs: np.ndarray, t0: int, t1: int, pitch: int) -> np.ndarray | None:
    """Cents deviation per frame for one note (NaN where the peak is weak).

    Starts at the expected frequency and follows the spectral peak frame by
    frame (max ±45 cents per frame) inside a band of -100..+320 cents, so bends
    up to a major third are followed while neighbouring chord tones are not
    jumped to.
    """
    best = None
    for h in (1, 2, 3):
        f = _midi_hz(pitch) * h
        if f * 2 ** (320 / 1200) > SR / 2 - 200:
            break
        bins = np.where((freqs >= f * 2 ** (-30 / 1200)) & (freqs <= f * 2 ** (30 / 1200)))[0]
        if len(bins) == 0:
            bins = np.array([int(round(f * N_FFT / SR))])
        energy = float(S[bins[0] - 1:bins[-1] + 2, t0:min(t1, t0 + 20)].mean())
        if best is None or energy > best[0]:
            best = (energy, h)
    if best is None:
        return None
    h = best[1]
    f_ref = _midi_hz(pitch) * h
    lo_b = max(1, int(np.floor(f_ref * 2 ** (-100 / 1200) * N_FFT / SR)))
    hi_b = min(S.shape[0] - 2, int(np.ceil(f_ref * 2 ** (320 / 1200) * N_FFT / SR)))
    seg = S[lo_b - 1:hi_b + 2, t0:t1]
    logs = np.log(seg + 1e-10)
    n_frames = seg.shape[1]
    cents = np.full(n_frames, np.nan)
    prev = 0.0
    for j in range(n_frames):
        col = logs[:, j]
        # candidate bins within ±45 cents of the previous estimate
        f_prev = f_ref * 2 ** (prev / 1200)
        k_lo = max(1, int(np.floor(f_prev * 2 ** (-45 / 1200) * N_FFT / SR)) - lo_b + 1)
        k_hi = min(len(col) - 2, int(np.ceil(f_prev * 2 ** (45 / 1200) * N_FFT / SR)) - lo_b + 1)
        if k_hi < k_lo:
            continue
        k = k_lo + int(np.argmax(col[k_lo:k_hi + 1]))
        a, b, c = col[k - 1], col[k], col[k + 1]
        d = a - 2 * b + c
        shift = float(np.clip(0.5 * (a - c) / d, -0.5, 0.5)) if abs(d) > 1e-9 else 0.0
        f_est = (lo_b - 1 + k + shift) * SR / N_FFT
        prominence = seg[k, j] / (np.median(seg[:, j]) + 1e-10)
        if prominence < 2.0:
            continue
        cents[j] = 1200 * np.log2(max(f_est, 1.0) / f_ref)
        prev = cents[j]
    return cents


def analyse_curve(cents: np.ndarray, fps: float) -> dict:
    """Classify a cents-deviation curve as vibrato / bend / steady."""
    out: dict = {}
    valid = ~np.isnan(cents)
    if valid.sum() < fps * 0.2:
        return {"reliable": False}
    x = cents.copy()
    # fill short gaps for spectral analysis
    idx = np.arange(len(x))
    x[~valid] = np.interp(idx[~valid], idx[valid], x[valid])
    out["reliable"] = bool(valid.mean() > 0.7)
    head = float(np.median(x[: max(3, int(0.06 * fps))]))
    tail = float(np.median(x[-max(3, int(0.12 * fps)):]))
    peak = float(np.max(x))
    # bend: start low, rise >= 70 cents and stay there
    rise = max(tail, float(np.percentile(x, 90))) - head
    bend = rise >= 70 and peak - head >= 70
    if bend:
        out["bend"] = {"cents": int(round(rise / 50) * 50), "release": bool(tail - head < 30)}
    # vibrato: detrend, look for a 4-9 Hz periodicity
    n = len(x)
    if n >= int(fps * 0.35) and not bend:
        t = np.arange(n)
        trend = np.polyval(np.polyfit(t, x, 2), t)
        r = x - trend
        win = np.hanning(n)
        spec = np.abs(np.fft.rfft(r * win, n=max(1024, n)))
        f = np.fft.rfftfreq(max(1024, n), d=1 / fps)
        band = (f >= 4) & (f <= 9)
        if band.any():
            k = np.argmax(np.where(band, spec, 0))
            rate = float(f[k])
            band_power = spec[band].max()
            ref = np.median(spec[(f > 1) & (f < 20)]) + 1e-9
            depth = float(np.percentile(r, 95) - np.percentile(r, 5)) / 2  # peak cents
            cycles = rate * n / fps
            conf = float(np.clip((band_power / ref - 2) / 6, 0, 1)) * float(np.clip((depth - 8) / 25, 0, 1))
            edge = rate <= 4.3 or rate >= 8.7  # band-edge peaks are beating/drift, not vibrato
            if depth >= 15 and cycles >= 2 and band_power / ref > 3 and not edge and valid.mean() > 0.8:
                out["vibrato"] = {"rate_hz": round(rate, 1), "depth_cents": int(depth),
                                  "confidence": round(conf, 2)}
    out["steady_cents"] = int(np.nanstd(cents))
    return out


def detect_expression(audio: str | Path, tr: Transcription, tracks: list[str] | None = None,
                      min_duration: float = 0.3) -> dict:
    import librosa

    audio = Path(audio)
    if not audio.exists():
        raise ArpeggioError(f"audio file not found: {audio}")
    log("loading audio for expression analysis")
    y, _ = librosa.load(str(audio), sr=SR, mono=True)
    S = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
    fps = SR / HOP
    results = []
    stats = {"analysed": 0, "vibrato": 0, "bend": 0, "unreliable": 0, "masked": 0}
    everyone = [(n, t.id) for t in tr.tracks if not t.is_drum for n in t.notes]
    everyone.sort(key=lambda x: x[0].start)
    starts = np.array([x[0].start for x in everyone]) if everyone else np.zeros(0)

    def interferers(n, tid):
        """Other notes overlapping in time (any track)."""
        hi = np.searchsorted(starts, n.end)
        return [m for m, mid in everyone[:hi] if m.end > n.start + 0.05 and not (m is n)]

    def masked(n, others) -> tuple[bool, bool]:
        """(vibrato_unsafe, bend_unsafe): another note's partial lies inside our tracking band."""
        vib_bad = bend_bad = False
        for h in (1, 2, 3):
            f = _midi_hz(n.pitch) * h
            for m in others:
                if m.pitch == n.pitch:
                    continue
                for k in range(1, 7):
                    c = 1200 * np.log2(_midi_hz(m.pitch) * k / f)
                    if -110 < c < 110:
                        vib_bad = True
                    if 30 < c < 340:
                        bend_bad = True
        return vib_bad, bend_bad
    for t in tr.tracks:
        if t.is_drum or (tracks and t.id not in tracks):
            continue
        log(f"analysing {t.id}")
        for n in t.notes:
            dur = n.end - n.start
            if dur < min_duration:
                continue
            f0 = int((n.start + 0.04) * fps)
            f1 = min(S.shape[1], int(n.end * fps))
            if f1 - f0 < fps * 0.2:
                continue
            curve = _track_note(S, freqs, f0, f1, n.pitch)
            if curve is None:
                continue
            res = analyse_curve(curve, fps)
            stats["analysed"] += 1
            vib_bad, bend_bad = masked(n, interferers(n, t.id))
            if vib_bad and "vibrato" in res:
                res.pop("vibrato")
                stats["masked"] += 1
            if bend_bad and "bend" in res:
                res.pop("bend")
                stats["masked"] += 1
            if not res.get("reliable"):
                stats["unreliable"] += 1
                continue
            if "vibrato" in res or "bend" in res:
                stats["vibrato"] += "vibrato" in res
                stats["bend"] += "bend" in res
                results.append({"track": t.id, "pitch": n.pitch, "start": round(n.start, 3),
                                "end": round(n.end, 3),
                                **{k: v for k, v in res.items() if k in ("vibrato", "bend")}})
    # a "bend" shared by several notes struck together is a tuning/beating artefact
    groups: dict = {}
    for r in results:
        if "bend" in r:
            groups.setdefault(round(r["start"], 2), []).append(r)
    for g in groups.values():
        if len(g) > 1:
            for r in g:
                r.pop("bend", None)
    results = [r for r in results if "vibrato" in r or "bend" in r]
    stats["vibrato"] = sum("vibrato" in r for r in results)
    stats["bend"] = sum("bend" in r for r in results)
    return {"kind": "expression", "source": {"path": str(audio.resolve()), "sha1": file_sha1(audio)},
            "method": "narrow-band harmonic peak tracking around transcribed notes",
            "stats": stats, "notes": results}
