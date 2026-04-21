#!/usr/bin/env python3
"""
POM Converter — native macOS Python port of Punk-O-Matic 2's POMConverter-v3.exe.

Converts POM2 song-data strings into MP3 files using ffmpeg (via pydub).
Input format (as accepted by the original .EXE, reverse-engineered from IL):

    PunkomaticSong(Title)DRUMS,GUITAR_A,BASS,GUITAR_B

Where each of the four track strings is a sequence of 2-character codes:
    "-X"   silence run of (char_val(X) + 1) slots  (1..52 slots)
    "XY"   riff at index char_val(X)*52 + char_val(Y) in that track's sample list
    "!!"   end-of-track marker (everything after is ignored)

char_val: 'a'..'z' → 0..25, 'A'..'Z' → 26..51.

A "slot" is exactly SamplesPerRiff (62259) samples at 44100 Hz = 1411.8 ms.
"""

from __future__ import annotations
import sys
import os
import re
import json
import argparse
import subprocess
import shutil
from pathlib import Path

try:
    from pydub import AudioSegment
except ImportError:
    print("ERROR: pydub not installed. Run: pip3 install pydub", file=sys.stderr)
    sys.exit(1)

# ── constants recovered from POMConverter-v3.exe .cctor ────────────────────────
# SamplesPerRiff (62259 @ 44100 Hz ≈ 1411.77 ms) is the length of one full
# pre-rendered riff sample (one bar of music). The grid "box" the encoding
# walks through, however, is half of that — two boxes per bar, ~705.87 ms —
# which matches the tempo of the Windows converter's output.
SAMPLES_PER_RIFF   = 62259
SAMPLES_PER_SLOT   = SAMPLES_PER_RIFF // 2   # 31129 samples per grid box
SAMPLES_MP3_OFFSET = 2383
DEFAULT_FREQUENCY  = 44100
SLOT_MS            = SAMPLES_PER_SLOT * 1000 / DEFAULT_FREQUENCY  # ≈ 705.87 ms
MP3_OFFSET_MS      = SAMPLES_MP3_OFFSET * 1000 / DEFAULT_FREQUENCY  # ≈ 54.04 ms

DRUM_VOLUME_DB        = 20 * __import__('math').log10(1.9)
BASS_VOLUME_DB        = 20 * __import__('math').log10(1.7)
GUITAR_VOLUME_DB      = 20 * __import__('math').log10(2.2)
LEAD_GUITAR_VOLUME_DB = 20 * __import__('math').log10(1.08)
MASTER_VOLUME_DB      = 20 * __import__('math').log10(0.8)
GUITAR_PANNING        = 0.75  # right channel for GuitarA, left for GuitarB

LEAD_START = 409  # guitarFiles[0..408]=rhythm, [409..800]=lead

# ── paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
OUTPUT_DIR = SCRIPT_DIR / "exportedSongs"

GUITAR_FILES: list[str] = []
DRUM_FILES:   list[str] = []
BASS_FILES:   list[str] = []
SAMPLE_CACHE: dict[str, AudioSegment | None] = {}


def _load_file_lists() -> None:
    """Load sample file lists from the embedded JSON extracted from the .EXE."""
    global GUITAR_FILES, DRUM_FILES, BASS_FILES
    json_path = SCRIPT_DIR / "pom_sample_lists.json"
    if json_path.exists():
        d = json.loads(json_path.read_text())
        GUITAR_FILES = d["guitar_files"]
        DRUM_FILES   = d["drum_files"]
        BASS_FILES   = d["bass_files"]
    else:
        _scan_data_dir()


def _scan_data_dir() -> None:
    global GUITAR_FILES, DRUM_FILES, BASS_FILES
    g = sorted((DATA_DIR / "Guitars").glob("*.mp3"), key=lambda p: p.name)
    d = sorted((DATA_DIR / "Drums").glob("*.mp3"),   key=lambda p: p.name)
    b = sorted((DATA_DIR / "Bass").glob("*.mp3"),    key=lambda p: p.name)
    GUITAR_FILES = ["data/Guitars/" + p.name for p in g]
    DRUM_FILES   = ["data/Drums/"   + p.name for p in d]
    BASS_FILES   = ["data/Bass/"    + p.name for p in b]


# ── character / index helpers (from GetValueFromDataChar IL) ──────────────────

def _char_val(c: str) -> int:
    """a-z → 0-25, A-Z → 26-51. Matches the .EXE's reimpl. of the encoding."""
    code = ord(c)
    if code >= 97:   # 'a'..'z'
        return code - 97
    return 26 + code - 65  # 'A'..'Z'


def riff_index(pair: str) -> int:
    return _char_val(pair[0]) * 52 + _char_val(pair[1])


def silence_run(c: str) -> int:
    return _char_val(c) + 1  # 1..52


# ── song-data parsing ──────────────────────────────────────────────────────────

_PUNK_PREFIX = re.compile(r'^\s*PunkomaticSong', re.IGNORECASE)


def clean_music_data(raw: str) -> list[str]:
    """
    Accept any of the following formats and return [title, drums, guitarA, bass, guitarB]:
      • PunkomaticSong(Title)DRUMS,GUITARA,BASS,GUITARB   (canonical, accepted by .EXE)
      • (Title)DRUMS,GUITARA,BASS,GUITARB
      • PunkomaticSong(Title)(DRUMS)(GUITARA)(BASS)(GUITARB)  (community paren format)
    """
    s = _PUNK_PREFIX.sub('', raw.strip())
    # Same stripping the .EXE's CleanMusicData does: remove whitespace, '?', '*'.
    s = re.sub(r'[\s?*]', '', s)

    # Case A: the canonical format — one title in parens, 4 comma-separated tracks.
    m = re.match(r'^\(([^)]*)\)(.*)$', s)
    if m:
        title = m.group(1)
        rest  = m.group(2)
        if ',' in rest:
            tracks = rest.split(',')
            return [title] + tracks
        # Case C: paren-delimited tracks. Extract remaining (...)
        paren_tracks = re.findall(r'\(([^)]*)\)', rest)
        if paren_tracks:
            return [title] + paren_tracks
        return [title, rest]

    # Fallback: no parens at all — assume fully comma-separated.
    parts = s.split(',')
    return parts


def parse_track(data: str) -> list[tuple[str, int]]:
    """
    Parse a track's riff string into a list of (kind, value) events.

    Encoding (from ConvertSong IL):
        • pair == '!!'        → end-of-track
        • pair[0] == '-'      → silence; count = _char_val(pair[1]) + 1
        • else                → riff at riff_index(pair)
    """
    events: list[tuple[str, int]] = []
    if not data:
        return events
    i = 0
    n = len(data)
    while i + 1 < n:
        pair = data[i:i+2]
        if pair == '!!':
            break
        if pair[0] == '-':
            events.append(('silence', silence_run(pair[1])))
        else:
            events.append(('riff', riff_index(pair)))
        i += 2
    return events


# ── audio helpers ──────────────────────────────────────────────────────────────

def load_sample(rel_path: str) -> AudioSegment | None:
    """Load a sample MP3 (cached). Strips the leading MP3 decoder padding."""
    if rel_path in SAMPLE_CACHE:
        return SAMPLE_CACHE[rel_path]
    full = SCRIPT_DIR / rel_path
    if not full.exists():
        SAMPLE_CACHE[rel_path] = None
        return None
    try:
        seg = AudioSegment.from_mp3(str(full))
        # Crop the first SAMPLES_MP3_OFFSET samples (the .EXE does the same).
        if len(seg) > MP3_OFFSET_MS:
            seg = seg[MP3_OFFSET_MS:]
        SAMPLE_CACHE[rel_path] = seg
        return seg
    except Exception as e:
        print(f"  Warning: could not load {rel_path}: {e}", file=sys.stderr)
        SAMPLE_CACHE[rel_path] = None
        return None


def lookup_sample(file_list: list[str], index: int) -> AudioSegment | None:
    if 0 <= index < len(file_list):
        return load_sample(file_list[index])
    return None


def _silence(ms: float) -> AudioSegment:
    return AudioSegment.silent(duration=int(round(ms)))


# ── rendering ──────────────────────────────────────────────────────────────────

_SLOT_MS_INT = int(round(SLOT_MS))
_RIFF_FADE_MS = 8  # tiny fade when a sample is cut by the next riff, to avoid clicks


def render_track(events: list[tuple[str, int]],
                 file_list: list[str]) -> AudioSegment:
    """
    Render a single track.

    Each "slot" is SamplesPerRiff (62259) samples at 44100 Hz = ~1411.77 ms.
    Behaviour (matches the Windows converter):
      • silence event → N slots of silence; does NOT stop the previous riff
      • riff event    → starts at its slot position and plays naturally;
                        it only gets cut short if another riff on the same
                        track starts before the sample ends.

    Concretely, each riff's effective length is the minimum of its own
    natural length and the distance (in slots) to the next riff on this
    track. If no further riff follows, the sample rings out to its end.
    """
    total_slots = 0
    for kind, val in events:
        total_slots += 1 if kind == 'riff' else val

    if total_slots == 0:
        return AudioSegment.silent(duration=0)

    # Locate each riff event's slot position; silence events just advance it.
    riffs: list[tuple[int, int]] = []  # (start_slot, sample_index)
    pos_slot = 0
    for kind, val in events:
        if kind == 'silence':
            pos_slot += val
        else:
            riffs.append((pos_slot, val))
            pos_slot += 1

    if not riffs:
        return AudioSegment.silent(duration=total_slots * _SLOT_MS_INT)

    # Start with a canvas sized for the slot grid; extend later if the final
    # sample rings out beyond the last slot.
    canvas_ms = total_slots * _SLOT_MS_INT
    canvas = AudioSegment.silent(duration=canvas_ms)

    for i, (start_slot, sample_idx) in enumerate(riffs):
        sample = lookup_sample(file_list, sample_idx)
        if sample is None or len(sample) == 0:
            continue

        # Hold for as long as possible: until the next riff on this track,
        # or forever (= natural sample length) if this is the last riff.
        if i + 1 < len(riffs):
            hold_slots = riffs[i + 1][0] - start_slot
            hold_ms = hold_slots * _SLOT_MS_INT
            if len(sample) > hold_ms:
                sample = sample[:hold_ms].fade_out(_RIFF_FADE_MS)
        # else: last riff on this track — let it ring out naturally.

        pos_ms = start_slot * _SLOT_MS_INT
        required_ms = pos_ms + len(sample)
        if required_ms > len(canvas):
            canvas += AudioSegment.silent(duration=required_ms - len(canvas))
        canvas = canvas.overlay(sample, position=pos_ms)

    return canvas


def mix_tracks(tracks: list[AudioSegment]) -> AudioSegment:
    if not tracks:
        return AudioSegment.silent(duration=0)
    max_len = max(len(t) for t in tracks) or 1
    canvas = AudioSegment.silent(duration=max_len)
    for t in tracks:
        if len(t) > 0:
            canvas = canvas.overlay(t)
    return canvas


# ── main conversion ────────────────────────────────────────────────────────────

def convert_song(song_data: str, output_dir: Path,
                 separate_tracks: bool = False,
                 quality: str = "standard") -> list[Path]:
    """
    Convert a POM2 song-data string into MP3 files.

    Section mapping (recovered from POMConverter-v3.exe IL):
        sections[0] → Title
        sections[1] → Drums        →  _Drum.mp3    (drumFiles)
        sections[2] → Guitar A     →  _GuitarA.mp3 (guitarFiles)
        sections[3] → Bass         →  _Bass.mp3    (bassFiles)
        sections[4] → Guitar B     →  _GuitarB.mp3 (guitarFiles)
    """
    sections = clean_music_data(song_data)
    if len(sections) < 2:
        raise ValueError("Could not parse song data: need a title and at least one track.")

    title    = sections[0].strip() if sections else "Unknown"
    drums    = sections[1] if len(sections) > 1 else ""
    guitar_a = sections[2] if len(sections) > 2 else ""
    bass     = sections[3] if len(sections) > 3 else ""
    guitar_b = sections[4] if len(sections) > 4 else ""

    safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "song"

    drum_events     = parse_track(drums)
    guitar_a_events = parse_track(guitar_a)
    bass_events     = parse_track(bass)
    guitar_b_events = parse_track(guitar_b)

    print(f"  Parsed: drums={len(drum_events)} evts, "
          f"guitarA={len(guitar_a_events)} evts, "
          f"bass={len(bass_events)} evts, "
          f"guitarB={len(guitar_b_events)} evts")

    drum_audio     = render_track(drum_events,     DRUM_FILES)     + DRUM_VOLUME_DB
    guitar_a_audio = render_track(guitar_a_events, GUITAR_FILES)   + GUITAR_VOLUME_DB
    bass_audio     = render_track(bass_events,     BASS_FILES)     + BASS_VOLUME_DB
    guitar_b_audio = render_track(guitar_b_events, GUITAR_FILES)   + GUITAR_VOLUME_DB

    # Apply left/right panning to the two guitars as the .EXE does.
    if len(guitar_a_audio) > 0:
        guitar_a_audio = guitar_a_audio.pan(+GUITAR_PANNING)  # right
    if len(guitar_b_audio) > 0:
        guitar_b_audio = guitar_b_audio.pan(-GUITAR_PANNING)  # left

    output_dir.mkdir(parents=True, exist_ok=True)
    out_files: list[Path] = []
    bitrate = {"standard": "192k", "extreme": "240k", "insane": "320k"}.get(quality, "192k")

    def _export(audio: AudioSegment, path: Path) -> None:
        if len(audio) == 0:
            return
        audio.export(str(path), format="mp3", bitrate=bitrate)
        out_files.append(path)
        print(f"  Saved: {path.name}")

    if separate_tracks:
        _export(drum_audio,     output_dir / f"{safe_title}_Drum.mp3")
        _export(guitar_a_audio, output_dir / f"{safe_title}_GuitarA.mp3")
        _export(guitar_b_audio, output_dir / f"{safe_title}_GuitarB.mp3")
        _export(bass_audio,     output_dir / f"{safe_title}_Bass.mp3")
    else:
        mixed = mix_tracks([drum_audio, guitar_a_audio, bass_audio, guitar_b_audio])
        if MASTER_VOLUME_DB:
            mixed = mixed + MASTER_VOLUME_DB
        _export(mixed, output_dir / f"{safe_title}.mp3")

    return out_files


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Punk-O-Matic 2 → MP3 converter (native macOS/Python)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 pom_converter.py
  python3 pom_converter.py "PunkomaticSong(MySong)DRUMS_CODE,GUITAR_A_CODE,BASS_CODE,GUITAR_B_CODE"
  python3 pom_converter.py --separate "PunkomaticSong(MySong)..."
  python3 pom_converter.py --output ~/Desktop --quality extreme "song_data"
""")
    parser.add_argument("song_data", nargs="?",
                        help="Song data string (if omitted, read interactively).")
    parser.add_argument("--separate", action="store_true",
                        help="Export each instrument as a separate MP3.")
    parser.add_argument("--output", default=str(OUTPUT_DIR),
                        help=f"Output directory (default: {OUTPUT_DIR}).")
    parser.add_argument("--quality", choices=["standard", "extreme", "insane"],
                        default="standard",
                        help="LAME encoder preset (default: standard ≈ 192 kbps).")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH. Install it with: brew install ffmpeg",
              file=sys.stderr)
        sys.exit(1)

    _load_file_lists()
    output_dir = Path(args.output).expanduser()

    if args.song_data:
        songs = [s.strip() for s in args.song_data.splitlines() if s.strip()]
    else:
        print("Punk-O-Matic 2 → MP3 Converter")
        print("Paste one or more song data strings, empty line to finish:\n")
        lines: list[str] = []
        try:
            while True:
                line = input()
                if not line.strip():
                    if lines:
                        break
                else:
                    lines.append(line.strip())
        except EOFError:
            pass
        songs = lines

    if not songs:
        print("No song data provided.")
        return

    total = 0
    for song in songs:
        if not song:
            continue
        print(f"\nConverting: {song[:60]}{'...' if len(song) > 60 else ''}")
        try:
            files = convert_song(song, output_dir,
                                 separate_tracks=args.separate,
                                 quality=args.quality)
            total += len(files)
        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)

    print(f"\nDone. {total} file(s) exported to {output_dir}")


if __name__ == "__main__":
    main()
