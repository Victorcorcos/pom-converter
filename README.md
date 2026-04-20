# POM Converter — Native macOS Port

A pure-Python, native-macOS port of **Punk-O-Matic 2's** song exporter
(`POMConverter-v3.exe`). Converts POM2 song-data strings into mixed `.mp3`
files without Wine, Mono, or `.NET`.

The original converter is a Windows `.NET 2.0` WinForms app that ships with
BASS.NET and LAME. It does not run on modern macOS (and the original
`READ_ME_FIRST.txt` says so plainly: *"The application will not work on unix
and mac operating systems either."*). This repo rebuilds the same conversion
pipeline in Python 3, driven by `ffmpeg` via `pydub`, so the same song strings
produce the same output on Apple Silicon.

---

## What is Punk-O-Matic 2?

[Punk-O-Matic 2](https://www.punk-o-matic.net/) is a browser-based music
creation game released in 2010. Players arrange pre-recorded guitar, bass,
and drum riffs on a grid to compose full songs. The game can export a song as
a short "song-data string" — a compact text encoding of every riff and
silence slot on every track. The stand-alone POM Converter turns that string
back into a mixed MP3.

A typical song-data string looks like:

```
PunkomaticSong(Honor)-Bdu-cdf-adg-adf-adu-adf-acJ-adf-adu-adf...
```

---

## Why a port?

- `POMConverter-v3.exe` is .NET 2.0 and depends on `bass.dll` / `Bass.Net.dll`
  (Windows-only native libraries). Wine on recent macOS is awkward to install
  and doesn't handle the native BASS libs cleanly.
- Mono on Apple Silicon has its own packaging issues and still can't load
  the Windows-native BASS DLLs.
- The song format is simple enough to port directly: four tracks of
  fixed-length slots, each slot either silent or filled by one of a few
  hundred pre-recorded MP3 samples. `ffmpeg` + `pydub` can overlay and mix
  them just fine.

Instead of emulating Windows we reverse-engineered the `.exe`'s IL with
[`dnfile`](https://github.com/malwarefrank/dnfile), recovered the encoding
rules and fixed-slot timing, and reimplemented them in roughly 400 lines of
Python.

---

## How it works

### Song-data grammar

After stripping the optional `PunkomaticSong` prefix and any whitespace,
`*`, or `?` characters, the game's `CleanMusicData()` routine produces:

```
(Title)DRUMS,GUITAR_A,BASS,GUITAR_B
```

Each track string is a sequence of 2-character codes:

| Pair      | Meaning                                                  |
| --------- | -------------------------------------------------------- |
| `!!`      | end-of-track marker (everything after is ignored)        |
| `-X`      | silence run of `char_val(X) + 1` slots (1..52)           |
| `XY`      | riff at index `char_val(X)*52 + char_val(Y)`             |

Where `char_val` maps `'a'..'z' → 0..25` and `'A'..'Z' → 26..51`. Recovered
from the IL of `GetValueFromDataChar`.

### Sample lookup

- **Drums track** → `drumFiles[i]` (179 samples)
- **Guitar A track** → `guitarFiles[i]` (rhythm, indices 0–408 of 801)
- **Bass track** → `bassFiles[i]` (396 samples)
- **Guitar B track** → `guitarFiles[i]` (lead, indices 409–800 of 801)

All 1,376 sample paths were extracted in original order from
`InitSampleFiles` and saved to `pom_sample_lists.json`.

### Fixed-slot timing

Recovered from the `POMConverter-v3.exe` static constructor:

| Constant            | Value    |
| ------------------- | -------- |
| `SamplesPerRiff`    | 62,259   |
| `SamplesMp3Offset`  | 2,383    |
| `DefaultFrequency`  | 44,100   |

Each slot is exactly `62259 / 44100 ≈ 1411.77 ms` wide. Samples are overlaid
at slot boundaries; samples longer than one slot bleed into the following
slots, which is how the game produces sustained-ringing parts. The first
`SamplesMp3Offset` samples of every clip are cropped (they are leading
decoder padding from the original MP3 encoder).

### Mixing

- Drums +5.6 dB, Bass +4.6 dB, Guitars +6.8 dB (pre-master).
- Guitar A panned 75% right, Guitar B panned 75% left.
- Final mix attenuated by the recovered master volume (~-1.9 dB).
- Exported at `standard` (192 k), `extreme` (240 k), or `insane` (320 k)
  LAME presets via `ffmpeg -b:a`.

See `PLAN.md` for the full reverse-engineering trail.

---

## Install

Requires macOS (tested on Sequoia 15.6, Apple Silicon) and Python 3.9+.

```bash
# 1. ffmpeg (needed by pydub for MP3 decode/encode)
brew install ffmpeg

# 2. Python dependency
pip3 install pydub
```

No virtualenv is required, but one is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pydub
```

---

## Usage

### One-shot from the command line

```bash
python3 pom_converter.py "PunkomaticSong(Honor)-Bdu-cdf-adg-..."
```

Produces `exportedSongs/Honor.mp3`.

### Separate per-instrument files

```bash
python3 pom_converter.py --separate "PunkomaticSong(Honor)..."
```

Produces:

- `exportedSongs/Honor_Drum.mp3`
- `exportedSongs/Honor_GuitarA.mp3` (rhythm)
- `exportedSongs/Honor_Bass.mp3`
- `exportedSongs/Honor_GuitarB.mp3` (lead)

### Other flags

```bash
python3 pom_converter.py \
    --output ~/Desktop/my-songs \
    --quality extreme \
    "(MySong)DRUMS,GUITAR_A,BASS,GUITAR_B"
```

- `--output DIR` — where to write the MP3s (default: `./exportedSongs`).
- `--quality {standard,extreme,insane}` — LAME preset (default: `standard`).
- `--separate` — export each instrument on its own instead of a mixed file.

### Interactive mode

Running with no arguments lets you paste one or more song strings, one per
line, and terminates on an empty line:

```bash
python3 pom_converter.py
```

### Double-click launcher (optional)

You can create a `.command` file so non-technical users can drag-drop song
strings onto it:

```bash
cat > run-pom-converter.command <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
python3 pom_converter.py
EOF
chmod +x run-pom-converter.command
```

---

## Repository layout

```
pom_converter.py          Main converter script (native Python port)
pom_sample_lists.json     Ordered sample paths extracted from the .EXE
PLAN.md                   Reverse-engineering notes and porting plan
data/
├── Guitars/*.mp3         801 guitar samples (rhythm 0–408, lead 409–800)
├── Drums/*.mp3           179 drum samples
└── Bass/*.mp3            396 bass samples
exportedSongs/            Default output folder (keeps reference renders)

# Windows originals — kept for reference and reverse-engineering parity.
POMConverter-v3.exe/.pdb  v3.1 converter (lantaren build, 2022)
POMConverter.exe/.pdb     Original v2 converter (2010)
Bass.Net.dll + bass.dll + bassmix.dll + bassenc.dll
lame.exe + lame_enc.dll
Changelog.txt
READ_ME_FIRST.txt
```

The Windows binaries and DLLs are intentionally kept in the repo so anyone
can reproduce the reverse-engineering work (e.g. re-run `dnfile` against
`POMConverter-v3.exe`). They are not executed by the Python port.

---

## How the port was derived

1. **Decompiled** `POMConverter-v3.exe` with `dnfile`, extracted the method
   table and string heap.
2. **Recovered** the char-encoding (`GetValueFromDataChar`, `GetNbEmptyBoxesFromCode`).
3. **Walked** `InitSampleFiles` IL (~15 KB of IL) to dump all 1,376 sample
   paths grouped by which file list (guitar/drum/bass) they loaded into.
4. **Disassembled** `ConvertSong` to find the four per-track processing
   blocks and confirm the rule is uniform: `pair[0] == '-'` → silence,
   else → riff.
5. **Extracted** the numeric constants (slot size, offset, track volumes,
   panning) from the `.cctor`.
6. **Verified** with an end-to-end conversion of the "Honor" song:
   duration 623.18 s, all four tracks aligned to exactly 440 slots, audio
   verified with `ffmpeg volumedetect`.

---

## Contributing

Contributions are welcome. This is a small project with a narrow scope, so
before opening a large PR please file an issue to discuss.

**Good contributions:**
- Bug fixes (wrong riff index, wrong timing, corrupted output, etc.).
- Audio-quality parity fixes (closer match to the original .EXE's render).
- Performance improvements for the slot-overlay pipeline.
- A small GUI or drag-drop launcher for macOS.
- Ports to Linux / Windows with Python — the logic is portable, only the
  ffmpeg install step changes.

**Please avoid:**
- Re-shipping the Windows DLLs or `.EXE` as executables — they are kept as
  reverse-engineering artifacts only.
- Large binary additions (new sample packs) — open an issue first.

### Development workflow

```bash
git clone <this repo>
cd pom-converter
python3 -m venv .venv && source .venv/bin/activate
pip install pydub

# Run against the Honor test string in PLAN.md
python3 pom_converter.py --separate "$(awk '/```$/{f=!f;next}f' PLAN.md)"
```

### Pull requests

1. Branch from `main`.
2. Keep the patch focused — one concern per PR.
3. Include a short description of what behaviour changes, and (for audio
   changes) an example song string + `ffprobe`/`volumedetect` readouts from
   before and after.
4. Do not commit generated MP3s unless they are an intentional reference
   render.

### License & credits

- Original Windows `POMConverter.exe` (2010) by the Punk-O-Matic 2 team.
- `POMConverter-v3.exe` (2022, v3.0/3.1) by **lantaren**
  (`lantaren@aim.com`, https://lantaren.bandcamp.com/).
- The `data/` sample packs are the property of the original Punk-O-Matic 2
  project and are included here under their original distribution terms for
  interoperability.
- This Python port is released to the public domain insofar as legally
  possible; see `PLAN.md` for the reverse-engineering trail.

If you are the original author of Punk-O-Matic 2 and have concerns about
redistribution, please open an issue.
