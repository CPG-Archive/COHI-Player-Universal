####################################################
# COHI-Player-Universal
# V1.2.0, 12.11.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import csv
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.request

from m3u_playlist import (
    Channel, PlaylistParseError, Track, apply_fileprefix, is_url,
    parse_combined_playlist_full, write_combined_playlist)
from fl2k_backend import _drain_pipe

logger = logging.getLogger(__name__)

_health_check_stop_event = None

DEFAULT_STATION_BANDWIDTH_HZ = 4500.0

REFERENCE_MODULATION_DEPTH = 0.8

LIVE_AUDIO_RATE = 22050

LIVE_UDP_BASE_PORT = 17000

LAST_STATIONS_MARKER_FILE = "cohi_stream_last_stations.txt"

LIVE_DECODER_STAGGER_S = 0.1

LOCAL_PLAYLIST_REPEAT_COUNT = 2000

LIVE_HEALTH_CHECK_INTERVAL_S = 15.0


class LiveStation:
    __slots__ = ("frequency_hz", "bandwidth_hz", "name", "url", "udp_port", "process",
                 "concat_file_path", "tracks", "entries", "silence_s")

    def __init__(self, frequency_hz, bandwidth_hz, name, url, udp_port):
        self.frequency_hz = frequency_hz
        self.bandwidth_hz = bandwidth_hz
        self.name = name
        self.url = url
        self.udp_port = udp_port
        self.tracks = None
        self.entries = None
        self.silence_s = 0.0
        self.process = None
        self.concat_file_path = None


def _parse_frequency_to_hz(text):
    text = text.strip()
    parts = text.split()
    if len(parts) != 2:
        raise ValueError(f"unexpected frequency format: {text!r}")
    value = float(parts[0].replace(",", "."))
    unit = parts[1].lower()
    multiplier = {"hz": 1, "khz": 1_000, "mhz": 1_000_000}.get(unit)
    if multiplier is None:
        raise ValueError(f"unknown unit in frequency value: {text!r}")
    return value * multiplier


class StationList(list):
    def __init__(self, iterable=(), params=None):
        super().__init__(iterable)
        self.params = params


def station_list_mod_depth(stations):
    params = getattr(stations, "params", None)
    return None if params is None else params.moddepth


def is_m3u_station_list(path):
    return str(path).lower().endswith((".m3u", ".m3u8"))


def inherit_playlist(new_station, old_station):
    if old_station is not None and old_station.tracks and new_station.url == old_station.url:
        new_station.tracks = old_station.tracks
        new_station.entries = old_station.entries
        new_station.silence_s = old_station.silence_s
    return new_station


def _load_station_list_m3u(path):
    try:
        try:
            channels, params = parse_combined_playlist_full(path, encoding="utf-8")
        except PlaylistParseError:
            channels, params = parse_combined_playlist_full(path, encoding="cp1252")
    except (OSError, PlaylistParseError) as exc:
        logger.error("COHI-Stream: station list %s could not be read: %s", path, exc)
        return StationList()

    for warning in params.warnings:
        logger.warning("station list %s: %s", path, warning)

    stations = StationList(params=params)
    for channel in channels:
        if channel.bandwidth_khz is not None:
            bandwidth_hz = channel.bandwidth_khz * 1000.0
        elif params.bw_khz is not None:
            bandwidth_hz = params.bw_khz * 1000.0
        else:
            bandwidth_hz = DEFAULT_STATION_BANDWIDTH_HZ
        entries = [apply_fileprefix(track.filename, params.fileprefix) for track in channel.tracks]
        station = LiveStation(channel.carrier_freq_hz, bandwidth_hz, channel.name, entries[0], 0)
        station.tracks = list(channel.tracks)
        station.entries = entries
        station.silence_s = params.silence_s or 0.0
        stations.append(station)

    reassign_ports(stations)
    logger.info("COHI-Stream: station list %s: %d stations loaded (M3U, %d entries, silence %.1f s)",
                path, len(stations), sum(len(st.entries) for st in stations),
                params.silence_s or 0.0)
    return stations


def load_station_list(csv_path):
    if is_m3u_station_list(csv_path):
        return _load_station_list_m3u(csv_path)
    stations = StationList()
    try:
        stream = open(csv_path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        logger.error("COHI-Stream: station list %s could not be opened: %s", csv_path, exc)
        return stations

    with stream:
        reader = csv.DictReader(stream, delimiter=";")
        for line_number, row in enumerate(reader, start=2):
            try:
                frequency_hz = _parse_frequency_to_hz(row["Frequenz"])
                bandwidth_hz = _parse_frequency_to_hz(row["Bandbreite"])
                name = row["Programmname"].strip()
                url = row["URL"].strip()
                if not url:
                    raise ValueError("empty URL")
            except (KeyError, ValueError) as exc:
                logger.warning("station list %s, line %d: skipped (%s)",
                                csv_path, line_number, exc)
                continue
            stations.append(LiveStation(frequency_hz, bandwidth_hz, name, url, 0))

    reassign_ports(stations)
    logger.info("COHI-Stream: station list %s: %d stations loaded", csv_path, len(stations))
    return stations


def reassign_ports(stations):
    for index, station in enumerate(stations):
        station.udp_port = LIVE_UDP_BASE_PORT + index


def _save_station_list_m3u(stations, path, params):
    channels = []
    for station in stations:
        tracks = station.tracks or [Track(title=station.name, filename=station.url)]
        bandwidth_khz = station.bandwidth_hz / 1000.0
        if params is not None and params.bw_khz is not None \
                and abs(bandwidth_khz - params.bw_khz) < 1e-6:
            bandwidth_khz = None
        channels.append(Channel(name=station.name, carrier_freq_hz=station.frequency_hz,
                                tracks=list(tracks), bandwidth_khz=bandwidth_khz))
    write_combined_playlist(channels, path, params=params)
    logger.info("COHI-Stream: station list with %d stations saved to %s (M3U)", len(stations), path)


def save_station_list(stations, csv_path, params=None):
    if is_m3u_station_list(csv_path):
        if params is None:
            params = getattr(stations, "params", None)
        _save_station_list_m3u(stations, csv_path, params)
        return
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Frequenz", "Bandbreite", "Programmname", "URL"])
        for station in stations:
            writer.writerow([
                f"{station.frequency_hz / 1000:.1f} kHz",
                f"{station.bandwidth_hz / 1000:.1f} kHz",
                station.name,
                station.url,
            ])
    logger.info("COHI-Stream: station list with %d stations saved to %s", len(stations), csv_path)


def read_last_stations_path():
    try:
        with open(LAST_STATIONS_MARKER_FILE, "r", encoding="utf-8") as stream:
            path = stream.read().strip()
    except OSError:
        return None
    return path or None


def write_last_stations_path(csv_path):
    try:
        with open(LAST_STATIONS_MARKER_FILE, "w", encoding="utf-8") as stream:
            stream.write(csv_path)
    except OSError as exc:
        logger.warning("COHI-Stream: last active path could not be remembered: %s", exc)


def _is_m3u(path_or_url):
    return path_or_url.lower().split("?", 1)[0].endswith((".m3u", ".m3u8"))


def _is_hls_manifest(content):
    return any(line.startswith("#EXT-X-") for line in content.splitlines())


class _HlsManifestDetected(Exception):
    pass


def _read_m3u_entries(source):
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=10) as response:
            content = response.read().decode("utf-8", errors="replace")
        base_dir = None
    else:
        with open(source, "r", encoding="utf-8", errors="replace") as stream:
            content = stream.read()
        base_dir = os.path.dirname(os.path.abspath(source))

    if _is_hls_manifest(content):
        raise _HlsManifestDetected()

    entries = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")) and base_dir and not os.path.isabs(line):
            line = os.path.join(base_dir, line)
        entries.append(line)
    return entries


def _resolve_stream_source(url):
    if not _is_m3u(url):
        return url, None

    try:
        entries = _read_m3u_entries(url)
    except _HlsManifestDetected:
        logger.info("COHI-Stream: %s recognized as a real HLS manifest - passed on unchanged "
                    "to ffmpeg (ffmpeg handles the HLS protocol itself)", url)
        return url, None
    except (OSError, ValueError) as exc:
        logger.warning("COHI-Stream: M3U %s could not be read/resolved: %s "
                           "- trying the original URL directly", url, exc)
        return url, None

    if not entries:
        logger.warning("COHI-Stream: M3U %s contained no valid entries", url)
        return url, None

    all_local = all(not e.startswith(("http://", "https://")) for e in entries)
    if all_local:
        return None, entries

    if not url.startswith(("http://", "https://")) and len(entries) >= 2:
        return None, entries
    return entries[0], None


_silence_files = {}


def _silence_file(seconds):
    key = round(float(seconds), 3)
    path = _silence_files.get(key)
    if path and os.path.isfile(path):
        return path
    handle = tempfile.NamedTemporaryFile(suffix=".mp3", prefix="cohi_silence_", delete=False)
    handle.close()
    command = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", f"anullsrc=r={LIVE_AUDIO_RATE}:cl=mono", "-t", str(key),
               "-b:a", "32k", handle.name]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=30)
        ok = result.returncode == 0 and os.path.getsize(handle.name) > 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("COHI-Stream: silence file could not be created: %s", exc)
        ok = False
    if not ok:
        logger.warning("COHI-Stream: silence between tracks (%.1f s) not available - "
                       "playlists run without gaps", key)
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        return None
    _silence_files[key] = handle.name
    return handle.name


def _build_input_args(station):
    if station.entries and (len(station.entries) >= 2 or not is_url(station.entries[0])):
        single, playlist = None, list(station.entries)
    elif station.entries:
        single, playlist = _resolve_stream_source(station.entries[0])
    else:
        single, playlist = _resolve_stream_source(station.url)

    if playlist:
        silence_file = _silence_file(station.silence_s) if station.silence_s > 0 else None
        if silence_file:
            silence_file = silence_file.replace("\\", "/")
        concat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ffconcat", delete=False, encoding="utf-8")
        for _ in range(LOCAL_PLAYLIST_REPEAT_COUNT):
            for entry in playlist:
                concat_file.write("file '{}'\n".format(entry.replace("'", r"'\''")))
                if silence_file:
                    concat_file.write("file '{}'\n".format(silence_file.replace("'", r"'\''")))
        concat_file.close()
        station.concat_file_path = concat_file.name
        logger.info("COHI-Stream: '%s' recognized as playlist (%d entries, repeated %dx)",
                     station.name, len(playlist), LOCAL_PLAYLIST_REPEAT_COUNT)
        return ["-re", "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
                "-f", "concat", "-safe", "0", "-i", concat_file.name]

    source = single
    if source.startswith(("http://", "https://")):
        return ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", source]
    return ["-i", source]


def _health_check_loop(stations, stop_event):
    already_reported = set()
    while not stop_event.wait(LIVE_HEALTH_CHECK_INTERVAL_S):
        for station in stations:
            if station.process is None or station.udp_port in already_reported:
                continue
            exit_code = station.process.poll()
            if exit_code is not None:
                logger.warning(
                    "COHI-Stream: decoder for '%s' (port %d) terminated unexpectedly "
                "(exit code %s) - this station no longer delivers audio",
                    station.name, station.udp_port, exit_code)
                already_reported.add(station.udp_port)


def _spawn_one_decoder(station):
    try:
        input_args = _build_input_args(station)
    except OSError as exc:
        logger.error("COHI-Stream: source for '%s' could not be resolved: %s",
                      station.name, exc)
        return
    command = [
        "ffmpeg", "-loglevel", "error", "-nostdin",
        "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
        *input_args,
        "-f", "u8", "-ar", str(LIVE_AUDIO_RATE), "-ac", "1",
        f"udp://127.0.0.1:{station.udp_port}",
    ]
    try:
        station.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _drain_pipe(station.process.stderr, f"ffmpeg-live:{station.name}")
        logger.info("COHI-Stream: decoder started for '%s' (%.0f Hz, UDP port %d, PID %s)",
                    station.name, station.frequency_hz, station.udp_port, station.process.pid)
    except OSError as exc:
        logger.error("COHI-Stream: decoder for '%s' could not be started: %s",
                      station.name, exc)


def restart_single_decoder(station):
    if station.process is not None and station.process.poll() is None:
        station.process.kill()
        try:
            station.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    station.process = None
    logger.warning("COHI-Stream: decoder for '%s' (port %d) is stuck (persistently starved despite a running "
                "process) - restarting",
                    station.name, station.udp_port)
    _spawn_one_decoder(station)


def start_station_decoders(stations):
    for index, station in enumerate(stations):
        if index > 0:
            time.sleep(LIVE_DECODER_STAGGER_S)
        _spawn_one_decoder(station)

    global _health_check_stop_event
    _health_check_stop_event = threading.Event()
    threading.Thread(target=_health_check_loop, args=(stations, _health_check_stop_event),
                      name="live-health-check", daemon=True).start()


def stop_station_decoders(stations):
    global _health_check_stop_event
    if _health_check_stop_event is not None:
        _health_check_stop_event.set()
        _health_check_stop_event = None

    for station in stations:
        if station.process is not None and station.process.poll() is None:
            station.process.kill()
            try:
                station.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        station.process = None

        if station.concat_file_path is not None:
            try:
                os.unlink(station.concat_file_path)
            except OSError:
                pass
            station.concat_file_path = None

    for silence_path in list(_silence_files.values()):
        try:
            os.unlink(silence_path)
        except OSError:
            pass
    _silence_files.clear()


def build_live_channel_args(stations):
    args = []
    for station in stations:
        args += ["--live-channel",
                  f"{station.udp_port}:{int(round(station.frequency_hz))}:{LIVE_AUDIO_RATE}"]
    return args
