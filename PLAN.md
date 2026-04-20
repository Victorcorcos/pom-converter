# POM Converter — Native macOS Port Plan

## Aim

Rebuild **Punk-O-Matic 2's** song exporter (`POMConverter-v3.exe`) as a native
macOS tool so it runs on **macOS Sequoia 15.6 (Apple Silicon, M4 Max)** without
Wine, Mono, or `.NET`.

The original program is a Windows `.NET 2.0` WinForms app that:

1. Accepts a POM2 song-data string (`PunkomaticSong(Title)(GuitA)(GuitB)(Bass)(Drums)`).
2. Parses each track into riffs / silences.
3. Looks up the matching pre-recorded `.mp3` samples in `data/Guitars`, `data/Drums`, `data/Bass`.
4. Mixes the four tracks with BASS.NET and writes `exportedSongs/<Title>.mp3`
   (or separate per-instrument files).

The port must produce the **same `.mp3`** given the same song string, using only
Python 3 + `pydub` + `ffmpeg` (already installed via Homebrew).

## Inputs available

| File | Purpose |
| --- | --- |
| `POMConverter-v3.exe` | Source of truth — reverse-engineered via `dnfile`. |
| `POMConverter-v3.pdb` | Symbol info (not yet parsed, only used for method names). |
| `data/Guitars/*.mp3` (801) | Guitar riff samples. Indices 0–408 = rhythm, 409–800 = lead. |
| `data/Drums/*.mp3` (179) | Drum samples. |
| `data/Bass/*.mp3` (396) | Bass samples. |
| `exportedSongs/Somethin_*.mp3` | Reference render for audio comparison. |

Environment (verified):
- `/opt/homebrew/bin/ffmpeg` installed
- `python3` with `pydub`, `dnfile` installed

## Reverse-engineering findings (done)

1. **Song-data grammar.** `PunkomaticSong(Title)(GuitA)(GuitB)(Bass)(Drums)`
   — extracted via `clean_music_data()` using a parenthesis regex. `!!` marks
   end-of-track inside a section.
2. **Character → index map.** `_char_val(c)`:
   `a..z → 0..25`, `A..Z → 26..51`. Derived from the IL of
   `GetValueFromDataChar` (RVA `0x39e4`).
3. **Riff lookup.** `get_value_from_data_char(s) = val(s[0]) * 52 + val(s[1])`.
4. **Silence run-length.** `get_nb_empty_boxes(c) = _char_val(c) + 1` → 1..52.
5. **Sample file tables.** Extracted in order by walking the IL of
   `InitSampleFiles` (RVA `0x428c`, 15 137 bytes) — every `ldstr` grouped by
   which `ldarg` (`1`=guitars, `2`=drums, `3`=bass) it was added to. Saved to
   `pom_sample_lists.json`.
6. **Guitar split.** Constant `LEAD_START = 409` — guitar indices
   `0..408` are rhythm, `409..800` are lead.

## What the current Python converter does (done)

`pom_converter.py` already implements:

- Parentheses-based song parsing (`clean_music_data`).
- Per-2-char riff/silence parser (`parse_track`) using the `char[0] == '-'`
  rule for **all four tracks** (this is the bug — see below).
- Sample loading with an in-memory cache (`load_sample`).
- Per-track rendering (`build_track_audio`) and 4-track overlay (`mix_tracks`).
- CLI: `python3 pom_converter.py`, `--separate`, `--output <dir>`.

Running now works without crashing, but the resulting audio is wrong for
at least the drum track (see next section).

## Known problems still to solve

### 1. Drum / lead-guitar encoding is wrong (blocker)

In the IL of `ConvertSong` (RVA `0x2250`) there are **four per-track processing
blocks**. The first block uses `bne.un.s` comparing `char[0]` against `'-'`
(45) → riff when `char[0] == '-'`. That is the rule used by `parse_track()`
today.

But at least one of the later blocks uses **`bge.s`** against `'-'`, which
inverts the test: a char is a **riff code** when `char[0] < '-'` (ASCII
punctuation like `(`, `)`, `*`, `+`, `,`). Under that rule:

- `(X` with `X='a'` → index `(*52 + 0 = (40-26)-…` — this can fall in the
  drum range 0–178.
- Chars `!` … `&` yield negative `val1`, which after two's-complement /
  wrapping could address `guitarFiles[409..800]` (lead guitar).

Until each block is mapped to its song section, the drum and lead tracks
render as silence.

### 2. Riff duration is guessed

`get_riff_duration_ms()` returns the length of the first loadable sample.
The real app almost certainly uses one slot length per "box" in the grid and
chops/pads samples to that length. We need to confirm from IL whether
samples are concatenated raw or time-quantised.

### 3. MP3 encoder quality flag

Changelog v3.0 mentions `standard` (~190 kbps) / `extreme` (~240 kbps) /
`insane` (320 kbps) LAME presets. The port currently exports with pydub's
default bitrate. Add a `--quality` flag that passes the right `-b:a` /
preset to ffmpeg.

## Steps already accomplished

- [x] Identified the failure mode of `wine POMConverter-v3.exe` on macOS.
- [x] Chose Python + pydub + ffmpeg as the native stack.
- [x] Decompiled `POMConverter-v3.exe` with `dnfile`, extracted method table.
- [x] Recovered `GetValueFromDataChar` and `GetNbEmptyBoxesFromCode` algorithms.
- [x] Walked `InitSampleFiles` IL and dumped all 1 376 sample paths (801/179/396) in order to `pom_sample_lists.json`.
- [x] Recovered the `PunkomaticSong(...)(...)(...)(...)(...)` grammar.
- [x] Wrote `pom_converter.py` with parser, sample cache, mixer, and CLI.
- [x] Verified the macOS environment has `ffmpeg` and `pydub`.

## Steps still to do

- [ ] **Finish disassembling `ConvertSong`.** Dump all four per-track blocks
      (offsets around 174, 864, …) as annotated IL, note the comparison
      opcode (`bne.un.s` vs `bge.s`) and which file list each block stores
      into.
- [ ] **Map section → (file-list, riff-detect rule).** Produce a table:
      `sections[1..4]` → which of `{guitarFiles (rhythm), guitarFiles (lead),
      bassFiles, drumFiles}` and whether riffs are detected by
      `char[0] == '-'` or `char[0] < '-'`.
- [ ] **Patch `parse_track(data, mode)`** in `pom_converter.py` to accept
      either detection rule, and update `convert_song` to call it with the
      right `mode` + file list per section.
- [ ] **Confirm riff slot duration.** Read the IL near the `BASS_ChannelPlay`
      / sample concatenation code to see if samples are padded to a fixed
      grid length.
- [ ] **Add `--quality {standard,extreme,insane}`** flag that maps to LAME
      presets via `ffmpeg -b:a`.
- [ ] **End-to-end test:** convert a known POM2 song string, compare the
      output to `exportedSongs/Somethin_*.mp3` (both audibly and via
      waveform / `ffprobe` duration).
- [ ] **Write a tiny macOS launcher** (shell script or `.command` file) so
      the user can double-click to run the converter against a string on
      the clipboard.

## Test section

Real song data for end-to-end testing — title "Honor", all four instruments present,
rhythm guitar in section 2 (indices 0–408) and lead guitar in section 4 (indices 409–800):

```
(Honor)-Bdu-cdf-adg-adf-adu-adf-acJ-adf-adu-adf-adg-adf-cdu-cdf-kdu-ccz-ccB-acB-acB-acB-acB-acB-acB-acB-acC-acD-acC-acJ-adf-cdo-cdo-cdo-gcY-ccY-ccY-ccY-ccY-ccY-ccY-ccJ-acK-ccK-ccC-acD-acC-acD-aaC-gcJ-adfcHdf-Edu-ccB-acBdpbE-gcC-acD-acC-acD-aaC-gcJ-adfcHdf-cdu-cdf-Z-Z-Z-y,dE-gdE-gdE-gdE-cdF-cdE-adl-adE-cbb-cbN-cdE-aeq-afv-gdE-cbN-cdE-cbN-cpm-cdp-acW-adp-caM-cby-cdp-aeb-aaM-abb-adE-cbN-cdE-cbN-gaN-abz-acl-bauaN-abz-acl-bauaN-abz-acm-bePfidrpt-cdu-adb-adu-cfl-bfqbD-cdu-aeg-afl-aaR-ack-adpdBeq-gdE-gdE-gdE-gdX-cbc-cdN-adu-adN-caf-bakbW-cdN-aez-aaf-abk-acI-adNdSeJ-gdX-Z-Z-Z-y,cx-gek-gek-cbk-cek-cbl-cbK-abk-aax-acx-aaK-acx-acXckbk-abK-abk-adKeK-bdx-ccx-ccX-cek-cek-chu-cbA-aba-aan-acn-adA-ccncaba-adA-aea-adA-aaK-aek-ccX-bcUek-ccX-caL-caK-abk-abK-baxaK-abk-abK-baxdK-aek-aeK-bdxdK-aek-ahu-abA-aba-aan-acn-aaA-baGcncaba-adA-aba-adA-aaA-aca-acnavbk-gek-cbk-cek-cbk-cek-cbk-cbx-cbn-cbN-abn-aaA-acA-aaN-baTcAcnbn-adQ-abq-adQ-aaQ-abQ-acDaGbx-cby-ccK-Z-Z-Z-y,-hkK-gkK-aloiulo-ckK-aloiulo-ckK-alo-aiu-ciJiujn-alo-ckK-alojnjRiu-fkK-aloiulo-ckK-aloiulo-cjb-cky-alc-aii-blSmliijbiilc-cjF-ajb-aoxml-akFkK-aloiulo-ckK-aloiulo-gcm-adrcYdr-bcYcm-adrcYdr-bcYcm-adrcYdr-bdhcv-adAdhkr-akCkylglcimkykCkyiBiijfiilg-cjJoxjfjbjJmhiBkykCiijfiilo-gkK-aloiulo-ckK-aloiulo-ckK-aloiulo-cjn-cjq-bjUkRlthXlTiBnHkRltiQmljumhhX-cjYjUjunRjYoXiQmGkRmljumhif-gkZ-Z-Z-Z-y
```

Run:

```
python3 pom_converter.py --separate "(Honor)..."
```

Expected outputs in `exportedSongs/`:
- `Honor_Drum.mp3`
- `Honor_GuitarA.mp3` (rhythm)
- `Honor_Bass.mp3`
- `Honor_GuitarB.mp3` (lead)

## How this maps to macOS Sequoia 15.6

- No Windows binaries are executed — the port is pure Python invoking
  `ffmpeg` under the hood via `pydub`.
- Apple Silicon–native: `ffmpeg` from Homebrew is `arm64`, `pydub` is pure
  Python, sample `.mp3`s decode through the system `ffmpeg`.
- No Gatekeeper / code-signing issues since nothing is a signed binary —
  the user runs `python3 pom_converter.py` from Terminal.
- Song data strings from <https://punk-o-matic.net> are passed as a CLI
  argument or pasted into interactive mode.
