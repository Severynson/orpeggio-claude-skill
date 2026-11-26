# Workflow examples

These are examples, not rigid pipelines. Pick the commands the request needs.
`W=arpeggio-work/<song>` below.

## A. Fingerstyle tab from an MP3 (classical, intermediate)

```bash
$ARP doctor
$ARP analyze-audio song.mp3                       # → $W/{rhythm,chords,notes,score,voices}.json
$ARP summary $W/score.json                        # key, tempo, chord grid, section hints
$ARP get-section $W/score.json --measures 1-8 --tracks voice,electric_bass
$ARP analyze-harmony $W/score.json --measures 1-16
$ARP suggest-key $W/voices.json
$ARP arrange-draft $W/voices.json --instrument classical_guitar --style arpeggio \
     --difficulty intermediate --transpose auto --title "Song" --artist "Band" -o $W/arrangement.json
$ARP export-text $W/arrangement.json --measures 1-8 --no-positions   # read, then rewrite
# … write $W/verse.atf with corrected measures …
$ARP patch-arrangement $W/arrangement.json $W/verse.atf
$ARP optimize-tablature $W/arrangement.json
$ARP validate-arrangement $W/arrangement.json
$ARP render-tab $W/arrangement.json --measures 1-8
$ARP export $W/arrangement.json --format gp5,pdf,musicxml,midi,ascii
```

Long song? Test perception on an excerpt first: `analyze-audio song.mp3
--start 30 --end 60 --workdir $W/excerpt`.

If the melody track is wrong: `extract-voices $W/score.json --melody voice
--bass electric_bass -o $W/voices.json`.

If MuScriptor is unavailable: `analyze-audio song.mp3 --backend pyin` (melody +
bass estimates only — warn the user it is much less accurate).

## B. Chords above lyrics

```bash
$ARP transcribe-lyrics song.mp3 --language en [--isolate-vocals]
# or, when the user supplies the words:  $ARP align-lyrics song.mp3 lyrics.txt
$ARP detect-chords song.mp3
$ARP align-chords-lyrics $W/chords.json $W/lyrics.json --title "Song"
```
Review the rendered sheet: merge one-beat chord blips, check chords against the
key (`detect-rhythm` + `quantize --chords` + `analyze-harmony` if unsure), mark
sections, then `export-chord-sheet $W/chord_sheet.json --format chordpro|pdf`.
See `chord-sheets.md`.

## C. Correct chords found online

```bash
$ARP parse-chord-sheet user_sheet.txt
$ARP detect-chords song.mp3
$ARP align-lyrics song.mp3 lyrics_only.txt      # lyrics text from the parsed sheet
$ARP analyze-audio song.mp3                     # notes, bass, score
$ARP analyze-harmony $W/score.json
```
Compare per line: user chord vs detected chord vs bass note vs melody notes.
Flag a change only with converging evidence; explain each suggestion; accept
equivalent readings (Am7 vs C/A, capo shapes vs concert pitch).

## D. Bass guitar tab (Guitar Pro)

```bash
$ARP transcribe-notes song.mp3 --instruments electric_bass
$ARP detect-rhythm song.mp3
$ARP quantize $W/notes.json --rhythm $W/rhythm.json --chords $W/chords.json
$ARP get-section $W/score.json --measures 1-8
```
Write an arrangement with `@instrument bass_guitar`, `@tuning bass_standard`,
role `bass` for every note (ATF), clean up ghost notes/octave errors, then
`optimize-tablature` → `validate-arrangement` → `export --format gp5,ascii`.

Sheet music for a non-fretted instrument (piano, violin…): quantize, write the
arrangement, skip tablature (`export --format musicxml,midi --force` — fretboard
validation does not apply).

## E. Isolate stems

`$ARP separate song.mp3 --stems vocals` (two-stem) or `--model htdemucs_6s`
for guitar/piano stems. Stems can be fed back to `transcribe-notes`,
`detect-chords` or `transcribe-lyrics` for cleaner results.
