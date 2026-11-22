####################################################
# COHI-Player-Universal
# Live streams (COHI-Stream, COHI-Player+Stream) for I/Q baseband backends
####################################################

# Mixin for backends that transfer complex baseband I/Q to the hardware
# (hwconfig: smisdr_iq). The internet stations are decoded by ffmpeg
# (live_streams), mixed by cohi_stemlab_live_upconverter into complex baseband
# (float32 I/Q, +-1.0 = full scale) around the LO frequency and passed on to a
# "sink" object, whose sendall() takes this float32 stream. In COHI-Player+Stream
# the baseband of the WAV file (also float32) is added in the same step.
#
# The logic follows the live part of stemlab_backend.py, but without its SSH /
# zeroconf parts, so smisdr_iq does not need those packages.

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time

import numpy as np
import psutil

import live_streams
from i18n import tr

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name != "posix"
DEFAULT_LIVE_UPCONVERTER_PATH = os.path.join(
    os.getcwd(),
    "cohi_stemlab_live_upconverter.exe" if _IS_WINDOWS else "cohi_stemlab_live_upconverter")

LIVE_TOOL_NAME = "cohi_stemlab_live_upconverter"
CHUNK_SAMPLES = 4096
CHUNK_BYTES = CHUNK_SAMPLES * 2 * 4          # float32 I/Q
PUMP_READ_BYTES = 65536
DIAG_LOG_INTERVAL_S = 10.0

# restart a decoder whose UDP port the upconverter reports as starved for
# several measuring windows in a row (see stemlab_backend)
STARVED_FRACTION_THRESHOLD = 0.8
STARVED_SAMPLES_PER_WINDOW_APPROX = 220500
STARVED_COUNT_THRESHOLD = int(STARVED_SAMPLES_PER_WINDOW_APPROX * STARVED_FRACTION_THRESHOLD)
STUCK_CONSECUTIVE_WINDOWS = 3

_AUSLASTUNG_LINE_RE = re.compile(
    r"hungrig gesamt: \d+, betroffene Ports \(Port:Anzahl\): (.*?), "
    r"(?:Live-Kanaele )?Ueberlauf gesamt:")

_DRAIN_LOG_WINDOW_S = 1.0
_DRAIN_LOG_BURST_THRESHOLD = 20


def _drain_pipe(pipe, label, line_callback=None):
    def _run():
        window_started_at = time.monotonic()
        lines_in_window = 0
        suppressed_in_window = 0
        try:
            for line in iter(pipe.readline, b""):
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                if line_callback is not None:
                    try:
                        line_callback(text)
                    except Exception:
                        logger.exception("error in line_callback for [%s]", label)
                now = time.monotonic()
                if now - window_started_at > _DRAIN_LOG_WINDOW_S:
                    if suppressed_in_window > 0:
                        logger.warning("[%s] ... %d more lines suppressed in this time window",
                                       label, suppressed_in_window)
                    window_started_at = now
                    lines_in_window = 0
                    suppressed_in_window = 0
                lines_in_window += 1
                if lines_in_window <= _DRAIN_LOG_BURST_THRESHOLD:
                    logger.warning("[%s] %s", label, text)
                else:
                    suppressed_in_window += 1
        except (OSError, ValueError):
            pass
    threading.Thread(target=_run, name=f"drain-{label}", daemon=True).start()


class ByteBuffer:
    """Thread-safe byte FIFO with limited size; stands in for a socket."""

    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self):
        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._closed = False

    def send(self, payload):
        data = bytes(payload)
        with self._condition:
            while len(self._buffer) >= self.MAX_BYTES and not self._closed:
                self._condition.wait(timeout=0.5)
            if self._closed:
                return len(data)
            self._buffer.extend(data)
            self._condition.notify_all()
        return len(data)

    def sendall(self, payload):
        self.send(payload)

    def read_exact_nonblocking(self, n):
        with self._condition:
            if len(self._buffer) >= n:
                data = bytes(self._buffer[:n])
                del self._buffer[:n]
                self._condition.notify_all()
                return data
            data = bytes(self._buffer)
            self._buffer.clear()
            self._condition.notify_all()
        return data + b"\x00" * (n - len(data))

    def read_exact_blocking(self, n, timeout=1.0):
        with self._condition:
            while len(self._buffer) < n:
                if self._closed or not self._condition.wait(timeout=timeout):
                    return None
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
            self._condition.notify_all()
            return data

    def fill_bytes(self):
        with self._condition:
            return len(self._buffer)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def settimeout(self, *args, **kwargs):
        pass


class IqLiveMixin:
    """Live part of a backend. The backend must provide errorOccurred, infoMessage,
    _current_params = (iq_sample_rate, lo_freq) and call _begin_live() / _end_live()."""

    def _init_live(self, live_enabled=False, program_variant="COHI-Player",
                   live_stations_file=None, live_upconverter_tool_path=None,
                   live_control_port=0, live_sferics_port=0):
        self.live_enabled = bool(live_enabled)
        self.program_variant = program_variant
        self.live_stations_file = live_stations_file
        self.live_upconverter_tool_path = live_upconverter_tool_path or DEFAULT_LIVE_UPCONVERTER_PATH
        self.live_control_port = int(live_control_port or 0)
        self.live_sferics_port = int(live_sferics_port or 0)
        self._live_stations = []
        self._live_process = None
        self._live_sink = None
        self._health_stop_event = None
        self._pump_stop_event = None
        self._combine_stop_event = None
        self._port_starved_streak = {}
        self._live_stopping = False

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------
    def _begin_live(self, lo_freq, iq_sample_rate, sink):
        """Starts decoders, upconverter and the data paths. Returns the buffer the WAV
        player has to write float32 I/Q into (COHI-Player+Stream), otherwise None."""
        self._live_stopping = False
        self._live_sink = sink
        stations = []
        if self.live_stations_file:
            try:
                stations = live_streams.load_station_list(self.live_stations_file)
            except OSError as exc:
                logger.error("%s: station list could not be loaded: %s", self.program_variant, exc)
                self.errorOccurred.emit(tr("station list could not be loaded: {error}").format(error=exc))
        self._live_stations = stations
        if stations:
            live_streams.start_station_decoders(stations)
        self._spawn_upconverter(lo_freq, iq_sample_rate, stations)

        if self.program_variant == "COHI-Player+Stream":
            wav_buffer = ByteBuffer()
            self._combine_stop_event = threading.Event()
            output_queue = ByteBuffer()
            threading.Thread(target=self._combine_loop,
                             args=(wav_buffer, output_queue, self._combine_stop_event, iq_sample_rate),
                             name="iq-combine", daemon=True).start()
            threading.Thread(target=self._network_send_loop,
                             args=(output_queue, sink, self._combine_stop_event),
                             name="iq-netsend", daemon=True).start()
            return wav_buffer
        if self._live_process is not None:
            self._start_pump()
        return None

    def _end_live(self):
        self._live_stopping = True
        if self._combine_stop_event is not None:
            self._combine_stop_event.set()
            self._combine_stop_event = None
        self._stop_upconverter()
        if self._live_stations:
            live_streams.stop_station_decoders(self._live_stations)
        self._live_stations = []
        self._live_sink = None

    def reload_live_stations(self, stations):
        if self._current_params is None:
            logger.warning("smisdr_iq: reload_live_stations() ignored without an active connection")
            return
        live_streams.stop_station_decoders(self._live_stations)
        live_streams.reassign_ports(stations)
        self._live_stations = stations
        if stations:
            live_streams.start_station_decoders(stations)
        self._stop_upconverter()
        iq_sample_rate, lo_freq = self._current_params
        self._spawn_upconverter(lo_freq, iq_sample_rate, self._live_stations)
        if self._live_process is not None and self.program_variant == "COHI-Stream":
            self._start_pump()

    # ------------------------------------------------------------------
    # upconverter process
    # ------------------------------------------------------------------
    def _upconverter_binary_available(self):
        command = self.live_upconverter_tool_path
        if os.path.isabs(command) or os.sep in command:
            return os.path.isfile(command)
        return shutil.which(command) is not None

    def _spawn_upconverter(self, lo_freq, iq_sample_rate, stations):
        if not stations:
            logger.info("%s: no stations in the station list - %s will not be started",
                        self.program_variant, LIVE_TOOL_NAME)
            return
        command = [self.live_upconverter_tool_path,
                   "--lo-freq", str(int(lo_freq)),
                   "--iq-sample-rate", str(int(iq_sample_rate))]
        for station in stations:
            command += ["--live-channel",
                        f"{station.udp_port}:{int(station.frequency_hz)}:{live_streams.LIVE_AUDIO_RATE}"]
        if self.live_control_port > 0:
            command += ["--live-control-port", str(self.live_control_port)]
        if self.live_sferics_port > 0:
            command += ["--live-sferics-port", str(self.live_sferics_port)]
        mod_depth = live_streams.station_list_mod_depth(stations)
        if mod_depth is not None:
            command += ["--modulation-depth", f"{mod_depth:.4f}"]
            logger.info("%s: modulation depth %.2f from the station list", self.program_variant, mod_depth)
        if not self._upconverter_binary_available():
            logger.error("%s: %s not found: %s", self.program_variant, LIVE_TOOL_NAME,
                         self.live_upconverter_tool_path)
            self.errorOccurred.emit(tr(
                "cohi_stemlab_live_upconverter not found: {path} "
                "(see cohi_stemlab_live_upconverter.cpp - must be compiled first, "
                "e.g. with 'make', or set stemlab_live_upconverter_path in config_sdr.yaml)"
            ).format(path=self.live_upconverter_tool_path))
            return
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.errorOccurred.emit(tr("cohi_stemlab_live_upconverter could not be started: {error}").format(error=exc))
            return
        _drain_pipe(process.stderr, LIVE_TOOL_NAME, line_callback=self._on_upconverter_stderr_line)
        self._live_process = process
        logger.info("%s: %s started (PID %s, %d stations)", self.program_variant, LIVE_TOOL_NAME,
                    process.pid, len(stations))
        self._raise_process_priority(process)
        self._start_health_check(process)

    @staticmethod
    def _raise_process_priority(process):
        try:
            proc = psutil.Process(process.pid)
            if _IS_WINDOWS:
                proc.nice(psutil.HIGH_PRIORITY_CLASS)
            else:
                proc.nice(-10)
        except psutil.Error as exc:
            logger.warning("process priority could not be set: %s", exc)

    def _start_health_check(self, process):
        stop_event = threading.Event()
        self._health_stop_event = stop_event

        def _run():
            while not stop_event.wait(timeout=0.5):
                returncode = process.poll()
                if returncode is None:
                    continue
                if stop_event.is_set():
                    return
                signal_detail = ""
                if returncode < 0:
                    try:
                        signal_detail = f" ({signal.Signals(-returncode).name})"
                    except ValueError:
                        signal_detail = f" (signal {-returncode})"
                logger.error("%s: %s (PID %s) terminated unexpectedly (return code %s%s)",
                             self.program_variant, LIVE_TOOL_NAME, process.pid, returncode, signal_detail)
                self.errorOccurred.emit(tr(
                    "cohi_stemlab_live_upconverter terminated unexpectedly "
                    "(return code {code}{detail})").format(code=returncode, detail=signal_detail))
                return
        threading.Thread(target=_run, name="iq-upconverter-health", daemon=True).start()

    def _stop_upconverter(self):
        if self._health_stop_event is not None:
            self._health_stop_event.set()
            self._health_stop_event = None
        if self._pump_stop_event is not None:
            self._pump_stop_event.set()
            self._pump_stop_event = None
        process = self._live_process
        self._live_process = None
        if process is not None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _on_upconverter_stderr_line(self, text):
        match = _AUSLASTUNG_LINE_RE.search(text)
        if match is None:
            return
        mentioned_ports = set()
        for entry in match.group(1).split(", "):
            if ":" not in entry:
                continue
            port_str, count_str = entry.rsplit(":", 1)
            try:
                port = int(port_str)
                count = int(count_str)
            except ValueError:
                continue
            mentioned_ports.add(port)
            if count >= STARVED_COUNT_THRESHOLD:
                self._port_starved_streak[port] = self._port_starved_streak.get(port, 0) + 1
            else:
                self._port_starved_streak.pop(port, None)
        for port in list(self._port_starved_streak):
            if port not in mentioned_ports:
                del self._port_starved_streak[port]
        for port in [p for p, s in self._port_starved_streak.items() if s >= STUCK_CONSECUTIVE_WINDOWS]:
            station = next((s for s in self._live_stations if s.udp_port == port), None)
            if station is not None:
                live_streams.restart_single_decoder(station)
            self._port_starved_streak.pop(port, None)

    # ------------------------------------------------------------------
    # data paths
    # ------------------------------------------------------------------
    def _start_pump(self):
        """COHI-Stream: upconverter stdout -> sink."""
        output_queue = ByteBuffer()
        stop_event = threading.Event()
        self._pump_stop_event = stop_event
        threading.Thread(target=self._pump_read_loop,
                         args=(self._live_process, output_queue, stop_event),
                         name="iq-live-pump-read", daemon=True).start()
        threading.Thread(target=self._network_send_loop,
                         args=(output_queue, self._live_sink, stop_event),
                         name="iq-live-pump-send", daemon=True).start()

    @staticmethod
    def _pump_read_loop(process, output_queue, stop_event):
        try:
            while not stop_event.is_set():
                chunk = process.stdout.read(PUMP_READ_BYTES)
                if not chunk:
                    break
                output_queue.send(chunk)
        except (OSError, ValueError):
            pass
        finally:
            output_queue.close()

    @staticmethod
    def _read_exact_from_process(process, n):
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                chunk = process.stdout.read(remaining)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) < n:
            data += b"\x00" * (n - len(data))
        return data

    def _combine_loop(self, wav_buffer, output_queue, stop_event, iq_sample_rate):
        """COHI-Player+Stream: WAV baseband + live baseband, in the real time of the I/Q rate."""
        stream_started_at = time.monotonic()
        samples_sent = 0
        while not stop_event.is_set():
            wav_bytes = wav_buffer.read_exact_nonblocking(CHUNK_BYTES)
            process = self._live_process
            if process is not None and process.poll() is None:
                live_bytes = self._read_exact_from_process(process, CHUNK_BYTES)
            else:
                live_bytes = b"\x00" * CHUNK_BYTES
            combined = (np.frombuffer(wav_bytes, dtype=np.float32)
                        + np.frombuffer(live_bytes, dtype=np.float32)).astype(np.float32)
            output_queue.send(combined.tobytes())

            samples_sent += CHUNK_SAMPLES
            sleep_s = stream_started_at + samples_sent / iq_sample_rate - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
        wav_buffer.close()
        output_queue.close()

    def _network_send_loop(self, output_queue, sink, stop_event):
        window_start = time.monotonic()
        send_count = 0
        send_time_total = 0.0
        send_time_max = 0.0
        while not stop_event.is_set():
            chunk = output_queue.read_exact_blocking(CHUNK_BYTES, timeout=1.0)
            if chunk is None:
                continue
            try:
                t_start = time.monotonic()
                sink.sendall(chunk)
                duration = time.monotonic() - t_start
            except OSError as exc:
                if not (stop_event.is_set() or self._live_stopping):
                    logger.error("%s: sending to the device failed: %s", self.program_variant, exc)
                    self.errorOccurred.emit(tr("sending to the smiSDR/parlioSDR failed: {error}").format(error=exc))
                break
            send_count += 1
            send_time_total += duration
            send_time_max = max(send_time_max, duration)
            now = time.monotonic()
            if now - window_start >= DIAG_LOG_INTERVAL_S:
                logger.info("[net_send_diag] %d sends in %.1fs, mean %.2f ms, max %.2f ms, queue %.0f%%",
                            send_count, now - window_start,
                            send_time_total / send_count * 1000, send_time_max * 1000,
                            output_queue.fill_bytes() / output_queue.MAX_BYTES * 100)
                window_start = now
                send_count = 0
                send_time_total = 0.0
                send_time_max = 0.0
