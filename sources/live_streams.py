####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
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

from fl2k_backend import _drain_pipe

logger = logging.getLogger(__name__)

_health_check_stop_event = None

LIVE_AUDIO_RATE = 22050

LIVE_UDP_BASE_PORT = 17000

LAST_STATIONS_MARKER_FILE = "cohi_stream_last_stations.txt"

LIVE_DECODER_STAGGER_S = 0.1

LOCAL_PLAYLIST_REPEAT_COUNT = 2000

LIVE_HEALTH_CHECK_INTERVAL_S = 15.0


class LiveStation:
    __slots__ = ("frequency_hz", "bandwidth_hz", "name", "url", "udp_port", "process",
                 "concat_file_path")

    def __init__(self, frequency_hz, bandwidth_hz, name, url, udp_port):
        self.frequency_hz = frequency_hz
        self.bandwidth_hz = bandwidth_hz
        self.name = name
        self.url = url
        self.udp_port = udp_port
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


def load_station_list(csv_path):
    stations = []
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


def save_station_list(stations, csv_path):
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
    return entries[0], None


def _build_input_args(station):
    single, playlist = _resolve_stream_source(station.url)

    if playlist:
        concat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ffconcat", delete=False, encoding="utf-8")
        for _ in range(LOCAL_PLAYLIST_REPEAT_COUNT):
            for entry in playlist:
                concat_file.write("file '{}'\n".format(entry.replace("'", r"'\''")))
        concat_file.close()
        station.concat_file_path = concat_file.name
        logger.info("COHI-Stream: '%s' recognized as local playlist (%d files, repeated %dx)",
                     station.name, len(playlist), LOCAL_PLAYLIST_REPEAT_COUNT)
        return ["-re", "-f", "concat", "-safe", "0", "-i", concat_file.name]

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


def build_live_channel_args(stations):
    args = []
    for station in stations:
        args += ["--live-channel",
                  f"{station.udp_port}:{int(round(station.frequency_hz))}:{LIVE_AUDIO_RATE}"]
    return args
