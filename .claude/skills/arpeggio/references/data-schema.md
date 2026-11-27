# Arpeggio data formats

All artifacts are JSON with a `kind`. Two time systems:

- **seconds** (`start`, `end`) from the start of the original recording — every
  perception output (transcription, rhythm, chords, lyrics).
- **musical time** after `quantize`: `measure` (1-based), `beat` (offset in the
  measure **in quarter notes**, 0 = downbeat), `duration` (quarter notes).
  Assumes the detected beat is a quarter note (time signature `N/4`). For 6/8 or
  half-time feels, re-run `detect-rhythm --beats-per-bar` and interpret accordingly.

Pitches are MIDI numbers (60 = C4) or names (`E4`, `F#3`, `Bb2`) — **sounding
pitch**. Guitar notation is written an octave higher; exporters handle that.

## kind: transcription (`transcribe-notes`)
```json
{"kind":"transcription","source":{"path":"…","sha1":"…","duration_seconds":187.3},
 "provenance":{"backend":"muscriptor","model":"muscriptor-medium"},
 "beat_grid":{"bpm":130.4,"beats_per_bar":4,"first_downbeat":0.12,"steady":true},
 "tracks":[{"id":"voice","instrument":"voice","program":52,"is_drum":false,
            "notes":[{"pitch":64,"start":0.95,"end":1.9,"velocity":80}]}]}
```

## kind: rhythm
`tempo_bpm`, `beats_per_bar`, `time_signature`, `steady_tempo`, `beats[]`,
`downbeats[]` (seconds), `tempo_curve[{time,bpm}]`.

## kind: chords
`chords[{chord:"Am", start, end, raw_label:"A:min"}]`. `N` = no chord.
Symbols: `C Cm C7 Cmaj7 Cm7 Cdim Caug Csus2 Csus4 C6 Cm6 C9 Cadd9 Cm7b5 Cdim7 C/E`.

## kind: lyrics
`language`, `text`, `lines[{text,start,end,words[{text,start,end,confidence?}]}]`.

## kind: score (`quantize`, `extract-voices`)
```json
{"kind":"score","tempo_bpm":130.4,"time_signature":"4/4","beats_per_bar":4,"key":"A minor",
 "measure_count":104,"measure_starts":[0.12,1.96,…],
 "tracks":[{"id":"melody","instrument":"melody","notes":[
     {"pitch":64,"name":"E4","measure":5,"beat":1.5,"duration":0.5,"start":9.1,"end":9.3}]}],
 "chords":[{"chord":"Am","measure":5,"beat":0,"duration":4}],
 "lyrics":[{"word":"stars","line":0,"measure":5,"beat":1.5}]}
```
`measure_starts[m-1]` maps a measure back to seconds in the recording.

## kind: arrangement (what you produce)
```json
{"kind":"arrangement","title":"…","artist":"…","instrument":"classical_guitar",
 "tuning":"standard","capo":0,"difficulty":"intermediate","tempo_bpm":92,
 "time_signature":"4/4","key":"A minor","measure_count":32,
 "sections":[{"name":"Verse","start_measure":1,"end_measure":8}],
 "chords":[{"chord":"Am","measure":1,"beat":0}],
 "notes":[{"pitch":"E4","measure":1,"beat":0,"duration":1,"role":"melody",
           "string":1,"fret":0,"locked":false,"voice":1,"technique":null}]}
```
- `lyrics` (optional, from `attach-lyrics`): `[{"text":"Они","measure":11,"beat":1.1,"line":0,"source":"aligned"}]`
  — one sung word per entry at its sung onset; `line` groups words into lyric lines.
- `role`: `melody` | `bass` | `harmony` | `counter` | `percussive`.
  Repair priority (what the optimiser sacrifices first): harmony → counter → bass → melody.
- `voice`: notation voice; default melody/counter → 1 (stems up), bass/harmony → 2.
- `string`: 1 = highest (high e). `fret` is relative to the capo.
- `tuning`: name (`standard`, `drop_d`, `dadgad`, `open_g`, …) or list low→high
  (`["D2","A2","D3","G3","B3","E4"]`).
- `technique`: one or more tokens joined by `+` (e.g. `h+vibrato`), describing how
  *this* note is produced: `vibrato`, `wide_vibrato`, `late_vibrato` (straight
  attack, wobble only after the last fretted note struck under it), `bend` (whole tone),
  `bend_half`, `bend_release`, `h` / `p` (hammer-on / pull-off **from the previous
  note on the same string**), `slide` (legato slide from the previous note on the
  same string), `slide_in`, `slide_out`, `harmonic`, `let_ring`, `palm_mute`,
  `staccato`, `ghost`, `accent`. Exporters move h/p/slide marks onto the origin
  note where Guitar Pro / MusicXML expect them.

## ATF — Arpeggio Text Format (compact arrangement text)

Use ATF to read and write arrangements: ~5× fewer tokens than JSON.

```
@title Budemo Razom
@artist Mandry
@instrument classical_guitar
@tuning standard
@capo 0
@difficulty intermediate
@tempo 92
@time 4/4
@key A minor
@section Verse 1-8
@note Melody up an octave in the chorus

M 1 Am | F@2
0    bass A2 2
0    mel  E4:1/0 1
1    mel  C5 .5
1.5  harm A3,C4 .5
2    bass F2 2 lock
2    mel  A4 2 tech=slide
```

- `#` at line start, or `//` anywhere, starts a comment (`#` elsewhere is a sharp).
- `M <measure> [chord[@beat] …]` starts a measure (`|` separators optional).
- Note line: `<beat> <role> <pitch[,pitch…]> <duration> [flags]`.
  - roles: `mel` `bass` `harm` `ctr` `perc`.
  - pitch: `E4`, or with position `E4:1/0` (string 1, fret 0), or `_:3/2`
    (pitch derived from string 3 fret 2).
  - duration in quarters: `4` whole, `2` half, `1` quarter, `.5` eighth,
    `.25` sixteenth, `1.5` dotted quarter, `.333` eighth-triplet.
  - flags: `lock` (optimizer keeps the given position), `tie`, `v1`/`v2`,
    `tech=…` (combine with `+`: `tech=h+vibrato`), `f=p|i|m|a` (right-hand finger).
- Notes held across a barline: write the note in its own measure with the full
  duration (e.g. beat 3, duration 2); exporters split and tie it.
- When patching, include **every** note of each measure you send — measures in
  the section file replace the old ones entirely. Lyrics are not part of ATF
  (`export-text` shows them as `# lyrics:` comments); patching keeps them.
