# Setup & troubleshooting

## Install (local)

```bash
cd <arpeggio repo>
uv sync --extra ml                 # MuScriptor, Beat This!, LV-Chordia (+ torch)
uv sync --extra ml --extra separation   # + Demucs
.claude/skills/arpeggio/scripts/arpeggio doctor
```

Global skill (all projects): `ln -s "$PWD/.claude/skills/arpeggio" ~/.claude/skills/arpeggio`
(the wrapper resolves the repo through the symlink, or set `ARPEGGIO_HOME`).

Requirements: Python 3.12, `ffmpeg`. Optional: MuseScore 4 (engraved PDFs;
set `ARPEGGIO_MUSESCORE` if not in a standard location).

## MuScriptor weights (gated, CC BY-NC 4.0)

1. Accept the licence on https://huggingface.co/MuScriptor/muscriptor-medium
   (and `-small` / `-large` if you use them).
2. `uvx hf auth login --force` (or export `HF_TOKEN`).
3. `doctor` → `"muscriptor_weights_access": "ok"`.

Non-commercial licence: fine for personal use; replace the backend before any
commercial use. Fallback without weights: `--backend pyin` (monophonic melody +
bass; much lower accuracy).

## ElevenLabs (lyrics)

Key in environment or repo `.env`: `ELEVENLABS_API_KEY=…` (also accepted:
`ELEVEN_LABS_API_KEY`). Uploads audio to ElevenLabs — mention it if the user's
audio is private. Never print the key.

## Caches & outputs

- Model outputs: `~/.cache/arpeggio/` (`ARPEGGIO_CACHE` to move). Delete to force
  recomputation, or pass `--no-cache` to `transcribe-notes`.
- Work files: `./arpeggio-work/<song>/` (`ARPEGGIO_WORKDIR` to move).
- Weights: `~/.cache/huggingface`, `~/.cache/torch/hub`.

## Cloud sessions

Works where Python 3.12 + network are available (Claude Code cloud with network
access). GPU is not guaranteed: use `--model small`, `--device cpu`, and short
excerpts (`--start/--end`) when slow. MuScriptor needs `HF_TOKEN` set as an
environment variable there.

## Common errors

| Error | Fix |
|---|---|
| `Cannot download MuScriptor weights` | Licence + HF login (above), or `--backend pyin` |
| `ElevenLabs API key missing` | add key to `.env` / env |
| `too few beats detected` | audio too short / rubato: pass `--beats-per-bar`, quantize coarser |
| `arrangement has N playability error(s)` on export | `validate-arrangement`, fix, retry |
| `Demucs not installed` | `uv sync --extra separation` |
