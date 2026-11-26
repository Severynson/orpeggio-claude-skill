# Arpeggio command reference

All commands: `$ARP <command> [args] [options]`. Output files default to
`./arpeggio-work/<song>/`. Every command prints `{"ok": true, ...}` JSON on
stdout (exceptions: `get-section`, `render-tab`, `export-text` print text).
Run `$ARP <command> --help` for every option.

## A. Audio perception (seconds timeline)

| Command | Purpose | Key options | Output |
|---|---|---|---|
| `doctor` | Check deps, HF model access, device, ElevenLabs key, MuseScore | — | stdout only |
| `analyze-audio AUDIO` | One shot: rhythm + chords + notes (+lyrics) → `score.json`, `voices.json` | `--backend muscriptor\|pyin`, `--model small\|medium\|large`, `--lyrics`, `--language`, `--start/--end`, `--beats-per-bar`, `--grid` | workdir with all files |
| `transcribe-notes AUDIO` | Multi-instrument notes (MuScriptor) + MIDI | `--instruments voice,electric_bass`, `--model`, `--constrain` (force decoding to `--instruments` instead of filtering afterwards — e.g. a Demucs vocal stem with `--instruments voice --constrain`), `--backend pyin` (fallback: melody+bass only), `--start/--end`, `--no-cache` | `notes.json`, `notes.mid` (kind `transcription`) |
| `detect-rhythm AUDIO` | Beats, downbeats, tempo, meter (Beat This!) | `--beats-per-bar N` to override meter | `rhythm.json` |
| `detect-chords AUDIO` | Chord segments (LV-Chordia; `--backend chroma` fallback) | `--vocabulary submission\|full\|extended`, `--min-duration` | `chords.json` |
| `transcribe-lyrics AUDIO` | Words + timestamps (ElevenLabs Scribe) | `--language uk`, `--isolate-vocals` (Demucs first) | `lyrics.json` |
| `align-lyrics AUDIO LYRICS.txt` | Forced alignment of known lyrics (keeps user text & line breaks) | `--isolate-vocals` | `lyrics.json` |
| `separate AUDIO` | Demucs stems | `--stems all\|vocals\|bass\|drums\|guitar\|piano`, `--model htdemucs_6s` | WAV paths |

MuScriptor instrument names: `voice acoustic_guitar clean_electric_guitar
distorted_electric_guitar electric_bass acoustic_bass acoustic_piano
electric_piano organ violin viola cello contrabass string_ensemble synth_strings
synth_lead synth_pad flutes clarinet oboe bassoon trumpet trombone tuba
french_horn brass_section soprano_and_alto_sax tenor_sax baritone_sax
orchestral_harp chromatic_percussion timpani orchestra_hit drums`.

Typical speed (M1 Pro): rhythm ~30 s, chords ~35 s per 3-min song;
MuScriptor medium on MPS roughly real-time-ish (use `--start/--end` to test a
30 s excerpt first on long songs). First runs download weights.

## B. Symbolic analysis

| Command | Purpose | Notes |
|---|---|---|
| `quantize NOTES --rhythm R [--chords C] [--lyrics L]` | seconds → measure/beat/duration using the beat grid (tempo drift OK) | `--grid 4` 16ths, `3` triplets, `12` both; drums excluded unless `--include-drums`. Output kind `score` |
| `extract-voices SCORE` | Skyline reduction to one `melody` + one `bass` track (chords kept) | `--melody voice` `--bass electric_bass` override auto choice; `--melody-min E3` |
| `analyze-harmony SCORE` | Key ranking, per-measure note-based chord candidates, bass, roman numerals, agreement with detected chords | `--measures 1-16`, `--resolution half`, `--tracks` |
| `summary FILE` | Level-1 overview of any artifact (chord grid by measure, repeated progressions = section hints) | |
| `get-section FILE --measures 9-16` / `--start 30 --end 45` | Compact text slice of score/arrangement/transcription/chords/lyrics | `--tracks melody,bass` |
| `align-chords-lyrics CHORDS LYRICS` | Chords attached to sung words → `chord_sheet.json` + rendered `.txt`/`.cho` | `--format text\|chordpro`, `--title` |
| `parse-chord-sheet FILE.txt` | Chords-over-lyrics or ChordPro → structured JSON | |

Score compact format (`get-section score.json`):
`M 5 Am F@2 (t=12.34s)` then `  voice: 0:E4/1 1:C5/.5 …` = beat:pitch/duration.

## C. Arrangement & tablature

| Command | Purpose |
|---|---|
| `list-tunings` | Tunings (standard, drop_d, dadgad, open_g, open_d, half_step_down, …) and difficulty limits |
| `chord-voicings Am,F,C,G [--capo 2 --tuning dadgad]` | Playable shapes, low→high string (`x32010`) |
| `suggest-key VOICES` | Guitar-friendly transpositions of the song key |
| `arrange-draft VOICES` | Baseline arrangement: melody on top, bass from chord roots/transcribed bass, fills. `--style arpeggio\|ballad\|travis\|sparse`, `--instrument classical_guitar\|acoustic_guitar`, `--difficulty`, `--tuning`, `--capo`, `--transpose N\|auto`, `--measures a-b`, `--title/--artist` |
| `export-text ARR [--measures a-b] [--no-positions]` | Arrangement → ATF text |
| `import-arrangement FILE.atf [--base ARR] -o ARR` | ATF → arrangement JSON |
| `patch-arrangement ARR SECTION.atf` | Replace the measures present in the section file (in place unless `-o`) |
| `transpose ARR --semitones N` | Transpose (clears tablature) |
| `optimize-tablature ARR` | Viterbi + refinement fingering. Options `--tuning --capo --difficulty --no-repair`. Reports `changes` (octave shifts, drops, shortened sustains, unlocks), `unplayable_groups`, fret range |
| `validate-arrangement ARR` | Errors/warnings/info with measure, beat and suggestion; full list in `<arr>.validation.json`. `--severity info` to see everything |
| `render-tab ARR [--measures a-b]` | ASCII tab to stdout for review (`h5` hammer-on, `p0` pull-off, `/7` slide, `5~` vibrato, `7b` bend, `<12>` harmonic) |
| `detect-expression AUDIO NOTES.json [--tracks ...]` | Measures real vibrato (rate Hz, depth cents) and bends of transcribed notes, by narrow-band tracking around each known pitch; masks notes whose band is polluted by other sounding notes. A stem (`separate`) gives cleaner evidence. Output `expression.json` |
| `suggest-techniques ARR [--expression E --score S --bar-offset N] [--apply evidence\|safe\|all] [--min-confidence 0.5] [--density-bars 2] [--no-idiomatic]` | Proposes techniques with `kind` (`evidence` / `idiomatic`), `confidence` and `reason`; never changes pitch or rhythm. Report in `<arr>.techniques.json`; `--apply` writes them into the arrangement. Needs tablature (run after `optimize-tablature`) |

Validation codes: `unassigned`, `pitch_mismatch`, `out_of_range`,
`string_collision`, `too_many_notes`, `stretch`, `fingers`, `beat_out_of_measure`
(errors); `high_fret`, `dense_chord`, `right_hand`, `cut_sustain`, `fast_shift`
(warnings); `melody_not_top`, `empty_measures` (info). Technique checks:
`open_string_effect`, `legato_without_source`, `legato_gap`, `hammer_direction`,
`pull_direction`, `legato_stretch`, `bend_low_string`, `unknown_technique`.

Difficulty limits: beginner span 3 / fret ≤7 / 3 notes at once; intermediate
span 4 / fret ≤12 / 4 notes; advanced span 5 / fret ≤17 / 6 notes.

## D. Export

| Command | Output |
|---|---|
| `export ARR --format gp5,musicxml,midi,ascii,pdf\|all [-d DIR] [--name N]` | Files in `<arr dir>/export/`. GP5 is read back to verify. Refuses if validation has errors (`--force` to override). PDF = MuseScore engraving when installed, else typeset ASCII tab |
| `export-chord-sheet chord_sheet.json --format text\|chordpro\|pdf` | Chord sheet files |
