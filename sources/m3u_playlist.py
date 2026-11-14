####################################################
# COHI-Player-Universal
# V1.2.0, 12.11.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

from dataclasses import dataclass, field
from i18n import tr
import os
import re
import subprocess


class PlaylistParseError(Exception):
    pass


@dataclass
class Track:
    title: str
    filename: str


@dataclass
class Channel:
    name: str
    carrier_freq_hz: float
    tracks: list = field(default_factory=list)
    bandwidth_khz: float = None


@dataclass
class PlaylistParams:
    reclength_s: float = None
    bw_khz: float = None
    outputfile: str = None
    fileprefix: str = None
    lofreq_khz: float = None
    samplerate_khz: float = None
    silence_s: float = None
    moddepth: float = None
    fileprefix_key: str = "FILEPREFIX"
    warnings: list = field(default_factory=list)

    @property
    def lofreq_hz(self):
        return None if self.lofreq_khz is None else self.lofreq_khz * 1000.0

    @property
    def samplerate_hz(self):
        return None if self.samplerate_khz is None else self.samplerate_khz * 1000.0

    @property
    def bw_hz(self):
        return None if self.bw_khz is None else self.bw_khz * 1000.0

    def is_empty(self):
        return all(getattr(self, name) is None for name in (
            "reclength_s", "bw_khz", "outputfile", "fileprefix", "lofreq_khz",
            "samplerate_khz", "silence_s", "moddepth"))


_EXTPARAM_RE = re.compile(r"^#EXTPARAM_([A-Za-z0-9_]+)\s*:\s*(.*)$")


def _parse_number(text):
    return float(text.strip().replace(",", "."))


def _parse_duration_seconds(text):
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(text)
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60.0 + _parse_number(part)
    return seconds


def _parse_mod_depth(text):
    value = _parse_number(text)
    if 1.0 < value <= 100.0:
        value /= 100.0
    if not 0.0 < value <= 1.0:
        raise ValueError(text)
    return value


def _positive(value):
    if value <= 0:
        raise ValueError(value)
    return value


def _apply_extparam(params, key, value, channel, line_no):
    key = key.upper()
    try:
        if channel is not None:
            if key == "BW":
                channel.bandwidth_khz = _positive(_parse_number(value))
            else:
                params.warnings.append(
                    f"line {line_no}: #EXTPARAM_{key} inside a station block ignored "
                    f"(only BW may be set per station)")
            return
        if key == "RECLENGTH":
            params.reclength_s = _positive(_parse_duration_seconds(value))
        elif key == "BW":
            params.bw_khz = _positive(_parse_number(value))
        elif key == "OUTPUTFILE":
            params.outputfile = value or None
        elif key in ("FILEPREFIX", "FILEPREXIX"):
            params.fileprefix = value or None
            params.fileprefix_key = key
        elif key == "LOFREQ":
            params.lofreq_khz = _positive(_parse_number(value))
        elif key == "SAMPLERATE":
            params.samplerate_khz = _positive(_parse_number(value))
        elif key == "SILENCE":
            silence = _parse_number(value)
            if silence < 0:
                raise ValueError(value)
            params.silence_s = silence
        elif key == "MODDEPTH":
            params.moddepth = _parse_mod_depth(value)
        else:
            params.warnings.append(f"line {line_no}: unknown parameter #EXTPARAM_{key} ignored")
    except ValueError:
        params.warnings.append(
            f"line {line_no}: invalid value '{value}' for #EXTPARAM_{key} - default used")


def is_url(text):
    return text.lower().startswith(("http://", "https://"))


def apply_fileprefix(entry, prefix):
    if not prefix or is_url(entry) or os.path.isabs(entry) or re.match(r"^[A-Za-z]:[\\/]", entry):
        return entry
    return prefix + entry


def parse_combined_playlist(path, encoding="utf-8"):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise PlaylistParseError(
            tr("File cannot be decoded as {encoding} ({error}) - if it is an older file "
               "created on Windows, try encoding=\"cp1252\"").format(encoding=encoding, error=exc)
        ) from exc
    return parse_combined_playlist_text(text)


def parse_combined_playlist_full(path, encoding="utf-8"):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise PlaylistParseError(
            tr("File cannot be decoded as {encoding} ({error}) - if it is an older file "
               "created on Windows, try encoding=\"cp1252\"").format(encoding=encoding, error=exc)
        ) from exc
    return parse_combined_playlist_text_full(text)


def parse_combined_playlist_text(text):
    return parse_combined_playlist_text_full(text)[0]


def parse_combined_playlist_text_full(text):
    lines = text.replace("\r\n", "\n").split("\n")

    if not any(line.strip() == "#EXTM3U" for line in lines[:3]):
        raise PlaylistParseError(
            tr("File does not start with #EXTM3U - not a valid extended M3U playlist "
               "(see m3u_playlist.py)"))

    channels = []
    params = PlaylistParams()
    current_channel = None
    pending_title = None

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line == "#EXTM3U":
            continue

        extparam = _EXTPARAM_RE.match(line)
        if extparam:
            _apply_extparam(params, extparam.group(1), extparam.group(2).strip(),
                            current_channel, line_no)
            continue

        if line.startswith("#PLAYLIST:"):
            name = line[len("#PLAYLIST:"):].strip()
            current_channel = Channel(name=name, carrier_freq_hz=None)
            channels.append(current_channel)
            pending_title = None

        elif line.startswith("#EXTGRP:"):
            if current_channel is None:
                raise PlaylistParseError(
                    tr("Line {line}: #EXTGRP without a preceding #PLAYLIST").format(line=line_no))
            freq_khz_text = line[len("#EXTGRP:"):].strip()
            try:
                freq_khz = float(freq_khz_text)
            except ValueError as exc:
                raise PlaylistParseError(
                    tr("Line {line}: #EXTGRP frequency '{value}' is not a number").format(
                        line=line_no, value=freq_khz_text)) from exc
            current_channel.carrier_freq_hz = freq_khz * 1000.0

        elif line.startswith("#EXTINF:"):
            _, _, title = line.partition(",")
            pending_title = title.strip()

        elif line.startswith("#"):
            continue

        else:
            if current_channel is None:
                raise PlaylistParseError(
                    tr("Line {line}: file name '{name}' without a preceding #PLAYLIST").format(
                        line=line_no, name=line))
            title = pending_title if pending_title is not None else line
            current_channel.tracks.append(Track(title=title, filename=line))
            pending_title = None

    for channel in channels:
        if channel.carrier_freq_hz is None:
            raise PlaylistParseError(
                tr("Station '{name}': no #EXTGRP (carrier frequency) given").format(name=channel.name))
        if not channel.tracks:
            raise PlaylistParseError(
                tr("Station '{name}': contains no tracks").format(name=channel.name))

    return channels, params


def _format_number(value):
    return str(int(value)) if value == int(value) else repr(float(value))


def _format_duration(seconds):
    total = int(round(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _param_lines(params):
    lines = []
    if params is None:
        return lines
    if params.reclength_s is not None:
        lines.append(f"#EXTPARAM_RECLENGTH: {_format_duration(params.reclength_s)}")
    if params.bw_khz is not None:
        lines.append(f"#EXTPARAM_BW: {_format_number(params.bw_khz)}")
    if params.outputfile:
        lines.append(f"#EXTPARAM_OUTPUTFILE: {params.outputfile}")
    if params.fileprefix:
        lines.append(f"#EXTPARAM_{params.fileprefix_key}: {params.fileprefix}")
    if params.lofreq_khz is not None:
        lines.append(f"#EXTPARAM_LOFREQ: {_format_number(params.lofreq_khz)}")
    if params.samplerate_khz is not None:
        lines.append(f"#EXTPARAM_SAMPLERATE: {_format_number(params.samplerate_khz)}")
    if params.silence_s is not None:
        lines.append(f"#EXTPARAM_SILENCE: {_format_number(params.silence_s)}")
    if params.moddepth is not None:
        lines.append(f"#EXTPARAM_MODDEPTH: {_format_number(params.moddepth)}")
    return lines


def write_combined_playlist(channels, path, encoding="utf-8", params=None):
    lines = ["#EXTM3U", ""]
    header = _param_lines(params)
    if header:
        lines += header + [""]
    for channel in channels:
        lines.append(f"#PLAYLIST: {channel.name}")
        if channel.bandwidth_khz is not None:
            lines.append(f"#EXTPARAM_BW: {_format_number(channel.bandwidth_khz)}")
        lines.append("")
        freq_khz = channel.carrier_freq_hz / 1000.0
        if freq_khz == int(freq_khz):
            lines.append(f"#EXTGRP: {int(freq_khz)}")
        else:
            lines.append(f"#EXTGRP: {freq_khz}")
        for track in channel.tracks:
            lines.append(f"#EXTINF: -1,{track.title}")
            lines.append(track.filename)
        lines.append("")

    text = "\r\n".join(lines) + "\r\n"
    with open(path, "wb") as f:
        f.write(text.encode(encoding))


def validate_channel(channel, base_dir):
    if channel.carrier_freq_hz is None or channel.carrier_freq_hz <= 0:
        return False, tr("invalid carrier frequency")
    if not channel.tracks:
        return False, tr("no tracks")
    missing = [t.filename for t in channel.tracks
               if not os.path.isfile(os.path.join(base_dir, t.filename))]
    if missing:
        if len(missing) == len(channel.tracks):
            return False, tr("all files missing")
        return False, tr("{missing}/{total} files missing").format(
            missing=len(missing), total=len(channel.tracks))
    return True, ""


def get_audio_duration_seconds(path, timeout=10):
    if not os.path.isfile(path):
        return None
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.decode("utf-8", errors="ignore").strip())
    except ValueError:
        return None


def compute_channel_duration_seconds(channel, base_dir):
    total = 0.0
    for track in channel.tracks:
        duration = get_audio_duration_seconds(os.path.join(base_dir, track.filename))
        if duration is not None:
            total += duration
    return total
