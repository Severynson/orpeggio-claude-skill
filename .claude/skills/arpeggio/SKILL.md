---
name: arpeggio
description: Musical intelligence toolkit for audio files (MP3/WAV/FLAC). Use whenever the user wants to transcribe a song or a specific instrument to notes/MIDI, detect chords, tempo or beats, get lyrics with chords above them, check or correct chord sheets, separate stems, or create guitar tablature — especially fingerstyle (acoustic or classical) guitar arrangements exported as Guitar Pro (.gp5), MusicXML, MIDI, ASCII tab or PDF. Provides a CLI for perception (MuScriptor, Beat This!, LV-Chordia, ElevenLabs, Demucs), analysis, fretboard optimisation and playability validation; Claude does the musical reasoning and arranging.
---

# Arpeggio

Arpeggio gives you **ears and hands**: commands that turn audio into structured
music data, and commands that turn your musical ideas into playable, validated
tablature and scores. **You are the musician**: you interpret the request, pick
the commands, read their outputs, make the creative and corrective decisions, and
iterate until the result is good.

## Running commands

```bash
ARP=".claude/skills/arpeggio/scripts/arpeggio"   # inside the arpeggio repo
# global install: ARP="$HOME/.claude/skills/arpeggio/scripts/arpeggio"
$ARP doctor            # first call in a session: deps, model access, device, API keys
$ARP <command> --help  # exact options
```

Every command prints a compact JSON result on stdout (`{"ok": true, "output": ...}`
or `{"ok": false, "error": ..., "hint": ...}`); progress goes to stderr. Full
results go to files, by default under `./arpeggio-work/<song>/`. Expensive model
outputs are cached by audio hash, so re-running is cheap — reuse files instead of
re-transcribing.

Full command reference: `references/commands.md`. Data formats (JSON artifacts
and the compact **ATF** arrangement text format): `references/data-schema.md`.

## Core principles

- **Compose small tools.** A request rarely maps to one command. Chords over lyrics
  needs lyrics + chords + alignment, not tablature. Call only what is needed.
- **One timeline.** All perception outputs use seconds from the start of the
  recording; `quantize` maps them to measures/beats. Combine outputs through it.
- **Transcriptions are estimates.** Cross-check notes vs chords vs bass vs key
  (`analyze-harmony`) and fix likely errors with musical judgement. Don't "fix"
  intentional dissonance.
- **Read hierarchically.** Start with `summary` (song overview), then
  `get-section --measures a-b` (compact text), and open raw JSON only if needed.
  Never dump a full transcription into context.
- **Validate before exporting.** Every instrumental arrangement goes through
  `optimize-tablature` → `validate-arrangement` → revise → … → `export`.
  `export` refuses arrangements with playability errors.
- **Never fabricate results.** If a model fails (e.g. gated weights, no API key),
  report it with the command's hint, or switch to the documented fallback
  (`--backend pyin`, `--backend chroma`) and say so. Don't invent notes or chords.
- **Respect the request**: tuning, capo, difficulty, instrument (classical vs
  acoustic), style, key and output formats.

## Workflow: fingerstyle guitar arrangement from an MP3 (main use case)

Read `references/fingerstyle.md` before arranging — it holds the musical
priorities and playability rules. Outline:

1. **Perceive** — `$ARP analyze-audio song.mp3` (rhythm, chords, MuScriptor notes,
   quantized `score.json`, and `voices.json` = one melody + one bass line).
   Check the result's `voices.melody_from` — is it really the lead (usually
   `voice`)? If not, rerun `extract-voices score.json --melody <track>`.
2. **Understand** — `summary score.json` (key, tempo, chord grid, repeated
   progressions = likely sections), `analyze-harmony score.json`, and
   `get-section score.json --measures 1-8` for sections you need to hear in detail.
   Decide sections (intro/verse/chorus…), key/capo (`suggest-key`), difficulty.
3. **Draft** — `$ARP arrange-draft voices.json --style arpeggio|ballad|travis|sparse
   --instrument classical_guitar --difficulty intermediate [--transpose auto]
   --title ... -o arrangement.json`. This is only a scaffold.
4. **Arrange (your job)** — section by section: `export-text arrangement.json
   --measures 1-8`, then rewrite the measures in ATF: correct melody errors, keep
   the melody on top, choose meaningful bass notes, shape the accompaniment
   pattern, simplify dense spots, keep repeated sections consistent. Write the
   section to a file and `patch-arrangement arrangement.json section.atf`.
   For short songs you may write the whole arrangement in ATF and
   `import-arrangement`.
5. **Fingering** — `optimize-tablature arrangement.json` (fills string/fret; may
   octave-shift or drop accompaniment notes and reports each change — review them).
6. **Validate & revise** — `validate-arrangement arrangement.json`. Fix every
   `error` (and important warnings) by editing the musical content, then repeat
   5–6. Use `lock` in ATF to pin positions you chose deliberately.
7. **Expression** (optional, recommended for intermediate+) — read the
   "Expressive techniques" section of `references/fingerstyle.md`.
   `detect-expression song.mp3 notes.json` measures real vibrato/bends in the
   recording; `suggest-techniques arrangement.json --expression expression.json
   --score score.json --bar-offset N` proposes vibrato, bends, slides,
   hammer-ons/pull-offs with reasons and confidence. Review them, apply with
   `--apply safe` (or edit `tech=` in ATF), then validate again. Low-confidence
   suggestions usually mean "nice, but simplify the accompaniment first".
8. **Lyrics** (default for songs with vocals; skip when the user asks for a
   version without lyrics) — timed words go under the tab in every format.
   - If the user gave the lyrics (in the prompt or a file), **always use them**:
     save them verbatim to `$W/user_lyrics.txt` (section headers like
     `[Chorus]` and lyrics-site junk such as "You might also like …" are
     stripped automatically) and run `sync-lyrics song.mp3 --text
     $W/user_lyrics.txt [--transcript $W/lyrics.json]`. The user's words are
     trusted; ElevenLabs only supplies timing.
   - Otherwise `sync-lyrics song.mp3 --language <code>` (ElevenLabs transcript;
     tell the user the words may contain mistakes and offer to use their text).
   - Then `attach-lyrics arrangement.json $W/lyrics_synced.json --score
     $W/score.json --bar-offset N` and check `by_measure`: do verses/choruses land
     in the right sections? `export-text` shows a `# lyrics:` comment per bar.
9. **Review** — `render-tab arrangement.json --measures 1-8` and read it as a
   guitarist: is the melody audible and recognisable, are shapes idiomatic?
10. **Export** — `$ARP export arrangement.json --format gp5,pdf,musicxml,midi,ascii`
   (lyrics included when attached; `--no-lyrics` for a version without them).
   Report file paths, key/capo/tuning, difficulty, and any compromises you made.
   For non-Latin lyrics tell the user the GP5 code page from the result
   (`gp5_encoding_note`).

## Other workflows (see `references/workflows.md`)

- **Chords above lyrics**: `sync-lyrics` (with `--text` if the user gave the
  words) + `detect-chords` → `align-chords-lyrics` → review → `export-chord-sheet`.
  Details: `references/chord-sheets.md`.
- **Correct an existing chord sheet**: `parse-chord-sheet` + `detect-chords` +
  `analyze-harmony` (+ `align-lyrics`) → compare evidence → corrected sheet with
  reasons. Both versions can be valid; say when.
- **Transcribe one instrument** (e.g. bass tab): `transcribe-notes --instruments
  electric_bass` + `detect-rhythm` → `quantize` → convert the track to an
  arrangement (`instrument: bass_guitar`, roles `bass`) → optimize → validate → export.
- **Stems**: `separate --stems vocals|bass|drums|guitar|piano`.

## Setup and failures

If `doctor` reports a problem, read `references/setup.md`. Common ones:
MuScriptor weights are gated (accept the licence on Hugging Face + `hf auth
login`); ElevenLabs needs `ELEVENLABS_API_KEY`/`ELEVEN_LABS_API_KEY` in the
environment or the repo `.env`; MuseScore is optional (engraved PDF — otherwise
PDF is a typeset ASCII tab). Never print or echo API keys.
