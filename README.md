# Arpeggio

A composable musical-intelligence toolkit, exposed as a **Python CLI** with a
**Claude Skill** on top (`.claude/skills/arpeggio/`). Claude orchestrates the
tools and does the musical reasoning; Arpeggio provides perception, analysis,
fretboard optimisation, playability validation and export.

Main use case today: **MP3 → fingerstyle guitar (classical or acoustic) tablature**
exported to Guitar Pro 5, MusicXML, MIDI, ASCII tab and PDF.

```
audio ──► analyze-audio ──► score.json / voices.json ──► arrange-draft ──► Claude edits (ATF)
            (MuScriptor, Beat This!,                         │
             LV-Chordia, ElevenLabs)                         ▼
                                    export ◄── validate-arrangement ◄── optimize-tablature
                              (gp5 · musicxml · midi · ascii · pdf)
```

## Install

```bash
uv sync --extra ml                     # core + MuScriptor, Beat This!, LV-Chordia
uv sync --extra ml --extra separation  # + Demucs stems
cp .env.example .env                   # add ELEVENLABS_API_KEY for lyrics
uv run arpeggio doctor
```

MuScriptor weights are gated (CC BY-NC 4.0): accept the licence at
https://huggingface.co/MuScriptor/muscriptor-medium and run `uvx hf auth login`.
Without them, `--backend pyin` gives a rough melody + bass. MuseScore 4 is optional
(engraved PDF; otherwise PDF = typeset ASCII tab).

## Use with Claude Code

Open Claude Code in this repo — the skill is picked up from `.claude/skills/arpeggio`.
For all projects: `ln -s "$PWD/.claude/skills/arpeggio" ~/.claude/skills/arpeggio`.
Then ask e.g. *"Make an intermediate classical-guitar fingerstyle arrangement of
data/test/song.mp3 and export Guitar Pro + PDF."*

## CLI overview

| Group | Commands |
|---|---|
| Perception | `analyze-audio`, `transcribe-notes`, `detect-rhythm`, `detect-chords`, `sync-lyrics`, `transcribe-lyrics`, `align-lyrics`, `separate`, `detect-expression` |
| Analysis | `quantize`, `extract-voices`, `analyze-harmony`, `summary`, `get-section`, `align-chords-lyrics`, `parse-chord-sheet` |
| Arrangement | `arrange-draft`, `suggest-techniques`, `attach-lyrics`, `import-arrangement`, `export-text`, `patch-arrangement`, `transpose`, `suggest-key`, `chord-voicings`, `list-tunings`, `optimize-tablature`, `validate-arrangement`, `render-tab` |
| Export | `export --format gp5,musicxml,midi,ascii,pdf [--no-lyrics]`, `export-chord-sheet` |

Details: [`.claude/skills/arpeggio/references/commands.md`](.claude/skills/arpeggio/references/commands.md),
data formats and the compact arrangement text format (ATF):
[`data-schema.md`](.claude/skills/arpeggio/references/data-schema.md).

## Layout

```
src/arpeggio/
  cli.py                 Typer CLI (JSON on stdout, progress on stderr)
  schema.py              shared representation (seconds + measures/beats)
  transcription/         notes (MuScriptor / pYIN), rhythm, chords, lyrics, separation
  analysis/              quantize, voices, harmony, chord sheets, sections/summary
  arrangement/           instruments, ATF text format, draft, tablature optimiser, validator
  export/                events (rests/ties), gp5, musicxml, midi/ascii/pdf
.claude/skills/arpeggio/ SKILL.md, scripts/arpeggio wrapper, references/
tests/                   pytest (symbolic pipeline, no models needed)
```

## Tests

```bash
uv run pytest
```

## Licences

Code: MIT-compatible dependencies, except model weights: MuScriptor weights are
CC BY-NC 4.0 (non-commercial). LV-Chordia MIT, Beat This! MIT, Demucs MIT.
