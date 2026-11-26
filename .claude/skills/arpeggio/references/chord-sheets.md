# Chord sheets

## Producing one

1. Words with timestamps: `transcribe-lyrics` (Scribe) — or `align-lyrics` when
   the user gives the lyrics (always prefer the user's words; alignment keeps
   their spelling and line breaks).
2. Chords with timestamps: `detect-chords` (LV-Chordia; `--vocabulary full` for
   jazz extensions; default `submission` is a good pop/rock vocabulary).
3. `align-chords-lyrics chords.json lyrics.json` attaches each chord change to
   the word sung when it happens (a chord starting up to 0.25 s early belongs to
   the next word). Changes during instrumental gaps are listed as `[Instrumental]`.
4. Review before presenting:
   - Collapse detector noise (a chord lasting < 1 beat between two identical
     chords is usually wrong).
   - Prefer simple, conventional spellings in the song key (`Bb` not `A#` in F major).
   - Extensions (7, maj7, sus) only when clearly audible / consistent.
   - Repeated sections should carry the same chords unless evidence differs.
   - Add section labels ([Verse], [Chorus]) from repetition of lyrics/chords.
5. Present as monospace chords-over-lyrics, or export:
   `export-chord-sheet chord_sheet.json --format text|chordpro|pdf`.

Format:

```
[Verse]
Am           F
The stars are shining,
C                G
Across the sky tonight.
```

Optional capo version: transpose chord names down by the capo fret and state
"Capo N" at the top.

## Checking an existing sheet

`parse-chord-sheet` returns lines with chords and character positions. Compare
each chord with detected chords around the aligned word time, the bass note and
melody notes in that measure (`analyze-harmony`). Report: line, original chord,
suggested chord, evidence, confidence. Do not flag musically equivalent
readings as errors.
