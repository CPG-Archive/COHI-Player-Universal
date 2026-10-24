####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
import re
import signal
import shutil
import socket
import subprocess
import threading
import time
from struct import pack

import numpy as np
import paramiko
import psutil
from zeroconf import ServiceBrowser, Zeroconf

import live_streams
from device_backend import DeviceBackend
from i18n import tr
from wav_tools import RPListener

logger = logging.getLogger(__name__)

SSH_PORT = 22
SSH_USERNAME = "root"
SSH_PASSWORD = "root"
STREAM_PORT = 1001

_IS_WINDOWS = os.name != "posix"
DEFAULT_LIVE_UPCONVERTER_PATH = os.path.join(
    os.getcwd(),
    "cohi_stemlab_live_upconverter.exe" if _IS_WINDOWS else "cohi_stemlab_live_upconverter")

BITS_PER_SAMPLE = 16
NORMALIZATION_FACTOR = (2 ** (BITS_PER_SAMPLE - 1)) - 1

STARVED_FRACTION_THRESHOLD = 0.8
STARVED_SAMPLES_PER_WINDOW_APPROX = 220500
STARVED_COUNT_THRESHOLD = int(STARVED_SAMPLES_PER_WINDOW_APPROX * STARVED_FRACTION_THRESHOLD)
STUCK_CONSECUTIVE_WINDOWS = 3

_AUSLASTUNG_LINE_RE = re.compile(
    r"hungrig gesamt: \d+, betroffene Ports \(Port:Anzahl\): (.*?), "
    r"(?:Live-Kanaele )?Ueberlauf gesamt:")

_DRAIN_LOG_WINDOW_S = 1.0
_DRAIN_LOG_BURST_THRESHOLD = 20


def _drain_pipe_stemlab(pipe, label, line_callback=None):
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


class _WavIQBuffer:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self):
        self._buffer = bytearray()
        self._condition = threading.Condition()

    def send(self, payload):
        data = bytes(payload)
        with self._condition:
            while len(self._buffer) >= self.MAX_BYTES:
                self._condition.wait(timeout=0.5)
            self._buffer.extend(data)
            self._condition.notify_all()

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
                if not self._condition.wait(timeout=timeout):
                    return None
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
            self._condition.notify_all()
            return data

    def close(self):
        pass

    def settimeout(self, *args, **kwargs):
        pass

    def fill_bytes(self):
        with self._condition:
            return len(self._buffer)


class _DebugTapSocket:
    def __init__(self, real_socket, backend):
        self._real_socket = real_socket
        self._backend = backend

    def send(self, payload):
        self._backend._maybe_call_debug_tap(bytes(payload))
        self._real_socket.sendall(bytes(payload))

    def sendall(self, payload):
        self.send(payload)

    def close(self):
        self._real_socket.close()

    def settimeout(self, *args, **kwargs):
        return self._real_socket.settimeout(*args, **kwargs)


class StemlabBackend(DeviceBackend):
    default_gain = 1.0

    def __init__(self, parent=None, live_enabled=False, program_variant="COHI-Player",
                 live_stations_file=None, live_upconverter_tool_path=None):
        super().__init__(parent)
        self.host_address = ""
        self.sample_rates = {}
        self.freq_correction_ppm = 0
        self.lo_offset = 0
        self._ssh = None
        self.ctrl_sock = None
        self.data_sock = None
        self._current_params = None

        self.live_enabled = live_enabled
        self.program_variant = program_variant
        self.live_stations_file = live_stations_file
        self.live_upconverter_tool_path = live_upconverter_tool_path or DEFAULT_LIVE_UPCONVERTER_PATH
        self._live_stations = []
        self._live_upconverter_process = None
        self._live_pump_thread = None
        self._live_pump_stop_event = None
        self._combine_thread = None
        self._network_send_thread = None
        self._combine_stop_event = None
        self._real_data_sock = None
        self._port_starved_streak = {}
        self._upconverter_health_stop_event = None
        self._debug_tap_callback = None
        self._debug_tap_last_sent = 0.0

    def set_debug_tap(self, callback):
        self._debug_tap_callback = callback

    def _maybe_call_debug_tap(self, chunk_bytes):
        if self._debug_tap_callback is None:
            return
        now = time.monotonic()
        if now - self._debug_tap_last_sent < 0.1:
            return
        self._debug_tap_last_sent = now
        try:
            arr = np.frombuffer(chunk_bytes, dtype=np.float32)
            iq = arr[0::2] + 1j * arr[1::2]
            lo_freq_hz = float(self._current_params[1]) if self._current_params else 0.0
            self._debug_tap_callback(iq, lo_freq_hz)
        except Exception:
            logger.exception("error in debug tap callback (STEMlab) - ignored")

    @staticmethod
    def identify():
        return {
            "rates": {20000: 0, 50000: 1, 100000: 2, 250000: 3,
                      500000: 4, 1250000: 5, 2500000: 6},
            "rate_type": "discrete",
            "RX": True,
            "TX": True,
            "device_name": "STEMlab 125-14",
            "max_IFREQ": 62500000,
            "min_IFREQ": 0,
            "resolutions": [16, 24, 32],
            "connection_type": "ethernet",
        }

    def connect_device(self, configured_address):
        address = configured_address
        if address and address != "0.0.0.0":
            self.host_address = address
            if self._ping():
                return address
            self.infoMessage.emit(tr("no response from {address}, searching network ...").format(address=address))

        found = self._search_devices()
        if not found:
            return None
        _, address = found[0]
        self.infoMessage.emit(tr("STEMlab found: {address}").format(address=address))
        self.host_address = address
        return address

    def _ping(self, timeout=3):
        try:
            with socket.create_connection((self.host_address, SSH_PORT), timeout=timeout):
                return True
        except OSError:
            return False

    def _search_devices(self):
        zeroconf = Zeroconf()
        listener = RPListener()
        ServiceBrowser(zeroconf, "_http._tcp.local.", listener)
        time.sleep(2)
        zeroconf.close()
        return listener.devices

    def configure_track(self, sample_rate, center_freq):
        if self.data_sock is not None and self._current_params == (sample_rate, center_freq):
            logger.info("STEMlab: same frequency/sample rate as the active connection - no reconfiguration needed")
            self.infoMessage.emit(tr("connection kept unchanged"))
            return True

        if self._current_params is not None:
            self._stop_combine_thread()
            self._stop_live_upconverter()
            if self._live_stations:
                live_streams.stop_station_decoders(self._live_stations)
            self._live_stations = []
            self._real_data_sock = None

        self.sample_rates = self.identify()["rates"]

        started, message = self._start_server()
        if not started:
            self.errorOccurred.emit(message)
            return False

        if not self._configure_socket(sample_rate, center_freq):
            return False

        self._current_params = (sample_rate, center_freq)

        if self.live_enabled and self.program_variant == "COHI-Stream":
            self._start_live_streams(center_freq, sample_rate)
        elif self.live_enabled and self.program_variant == "COHI-Player+Stream":
            self._start_live_streams_combined(center_freq, sample_rate)
        else:
            self.data_sock = _DebugTapSocket(self.data_sock, self)

        return True

    def _start_server(self):
        if not self._connect_ssh():
            return False, tr("no connection to the STEMlab - server could not be started")

        self._stop_server()
        time.sleep(0.2)
        self._run_ssh_commands(["/bin/bash /sdrstart.sh &"])
        time.sleep(0.2)
        return True, ""

    def _stop_server(self):
        self._run_ssh_commands(["/bin/bash /sdrstop.sh &"])

    def _connect_ssh(self):
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._ssh.connect(self.host_address, SSH_PORT, SSH_USERNAME, SSH_PASSWORD)
            return True
        except (paramiko.SSHException, OSError) as exc:
            self.errorOccurred.emit(tr("SSH connection to {address} failed: {error}").format(
                address=self.host_address, error=exc))
            return False

    def _run_ssh_commands(self, commands):
        for command in commands:
            try:
                self._ssh.exec_command(command)
            except paramiko.SSHException as exc:
                logger.error("SSH command failed (%s): %s", command, exc)
            time.sleep(0.1)

    def _configure_socket(self, sample_rate, center_freq):
        self.ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ctrl_sock.settimeout(5)
        self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.data_sock.settimeout(5)
        self.data_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.ctrl_sock.connect((self.host_address, STREAM_PORT))
            self.data_sock.connect((self.host_address, STREAM_PORT))
        except OSError as exc:
            self.errorOccurred.emit(tr("socket connection to the STEMlab failed: {error}").format(error=exc))
            return False

        rate_index = self.sample_rates[sample_rate]
        effective_lo = int((1.0 + 1e-6 * self.freq_correction_ppm) * center_freq + self.lo_offset)

        self.ctrl_sock.send(pack("<I", 2))
        self.ctrl_sock.send(pack("<I", 0 << 28 | effective_lo))
        self.ctrl_sock.send(pack("<I", 1 << 28 | rate_index))
        self.data_sock.send(pack("<I", 3))

        self.infoMessage.emit(tr("socket streaming started"))
        return True

    def encode_samples(self, samples, gain):
        return gain * samples.astype(np.float32) / NORMALIZATION_FACTOR

    def _start_live_streams(self, lo_freq, iq_sample_rate):
        stations = []
        if self.live_stations_file:
            try:
                stations = live_streams.load_station_list(self.live_stations_file)
            except OSError as exc:
                logger.error("COHI-Stream (STEMlab): station list could not be loaded: %s", exc)
                self.errorOccurred.emit(tr("station list could not be loaded: {error}").format(error=exc))
        self._live_stations = stations
        if stations:
            live_streams.start_station_decoders(stations)
        self._spawn_live_upconverter(lo_freq, iq_sample_rate, stations)
        if self._live_upconverter_process is not None:
            self._start_live_pump_thread()

    def _start_live_streams_combined(self, lo_freq, iq_sample_rate):
        stations = []
        if self.live_stations_file:
            try:
                stations = live_streams.load_station_list(self.live_stations_file)
            except OSError as exc:
                logger.error("COHI-Stream (STEMlab): station list could not be loaded: %s", exc)
                self.errorOccurred.emit(tr("station list could not be loaded: {error}").format(error=exc))
        self._live_stations = stations
        if stations:
            live_streams.start_station_decoders(stations)
        self._spawn_live_upconverter(lo_freq, iq_sample_rate, stations)

        if self._live_upconverter_process is not None:
            real_socket = self.data_sock
            self._real_data_sock = real_socket
            self.data_sock = _WavIQBuffer()
            output_queue = _WavIQBuffer()
            self._combine_stop_event = threading.Event()
            self._combine_thread = threading.Thread(
                target=self._combine_loop,
                args=(self.data_sock, output_queue, self._combine_stop_event, iq_sample_rate),
                name="stemlab-combine", daemon=True)
            self._combine_thread.start()
            self._network_send_thread = threading.Thread(
                target=self._network_send_loop,
                args=(output_queue, real_socket, self._combine_stop_event),
                name="stemlab-netsend", daemon=True)
            self._network_send_thread.start()

    def _combine_loop(self, wav_buffer, output_queue, stop_event, iq_sample_rate):
        CHUNK_SAMPLES = 4096
        CHUNK_BYTES = CHUNK_SAMPLES * 2 * 4

        stream_started_at = time.monotonic()
        samples_sent = 0

        while not stop_event.is_set():
            wav_bytes = wav_buffer.read_exact_nonblocking(CHUNK_BYTES)

            process = self._live_upconverter_process
            if process is not None and process.poll() is None:
                live_bytes = self._read_exact_from_process(process, CHUNK_BYTES)
            else:
                live_bytes = b"\x00" * CHUNK_BYTES

            wav_arr = np.frombuffer(wav_bytes, dtype=np.float32)
            live_arr = np.frombuffer(live_bytes, dtype=np.float32)
            combined = (wav_arr + live_arr).astype(np.float32)

            output_queue.send(combined.tobytes())

            samples_sent += CHUNK_SAMPLES
            due_at = stream_started_at + samples_sent / iq_sample_rate
            sleep_s = due_at - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)

    def _network_send_loop(self, output_queue, real_socket, stop_event):
        CHUNK_BYTES = 4096 * 2 * 4
        DIAG_LOG_INTERVAL_S = 10.0
        window_start = time.monotonic()
        send_count = 0
        send_time_total = 0.0
        send_time_max = 0.0

        while not stop_event.is_set():
            chunk = output_queue.read_exact_blocking(CHUNK_BYTES, timeout=1.0)
            if chunk is None:
                continue
            self._maybe_call_debug_tap(chunk)
            try:
                t_send_start = time.monotonic()
                real_socket.sendall(chunk)
                send_duration = time.monotonic() - t_send_start
            except OSError as exc:
                logger.error("COHI-Player+Stream (STEMlab): sending to data_sock failed: %s", exc)
                break

            send_count += 1
            send_time_total += send_duration
            send_time_max = max(send_time_max, send_duration)

            now = time.monotonic()
            if now - window_start >= DIAG_LOG_INTERVAL_S:
                avg_ms = (send_time_total / send_count * 1000) if send_count else 0.0
                fill_bytes = output_queue.fill_bytes() if hasattr(output_queue, "fill_bytes") else -1
                max_bytes = getattr(output_queue, "MAX_BYTES", -1)
                fill_pct = (fill_bytes / max_bytes * 100) if fill_bytes >= 0 and max_bytes > 0 else -1.0
                logger.warning(
                    "[net_send_diag] %d send operations in %.1fs, sendall() mean %.2fms, "
                    "maximum %.2fms, queue fill level %.0f%% (%d/%d bytes)",
                    send_count, now - window_start, avg_ms, send_time_max * 1000,
                    fill_pct, fill_bytes, max_bytes)
                window_start = now
                send_count = 0
                send_time_total = 0.0
                send_time_max = 0.0

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

    def _stop_combine_thread(self):
        if self._combine_stop_event is not None:
            self._combine_stop_event.set()
        self._combine_thread = None
        self._network_send_thread = None
        self._combine_stop_event = None

    def _upconverter_binary_available(self):
        command = self.live_upconverter_tool_path
        if os.path.isabs(command) or os.sep in command:
            return os.path.isfile(command)
        return shutil.which(command) is not None

    def _spawn_live_upconverter(self, lo_freq, iq_sample_rate, stations):
        if not stations:
            logger.info("COHI-Stream (STEMlab): no stations in the station list - "
                "cohi_stemlab_live_upconverter will not be started")
            return
        command = [self.live_upconverter_tool_path,
                   "--lo-freq", str(int(lo_freq)),
                   "--iq-sample-rate", str(int(iq_sample_rate))]
        for station in stations:
            command += ["--live-channel",
                        f"{station.udp_port}:{int(station.frequency_hz)}:{live_streams.LIVE_AUDIO_RATE}"]
        if not self._upconverter_binary_available():
            logger.error("COHI-Stream (STEMlab): cohi_stemlab_live_upconverter not found: %s",
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
        _drain_pipe_stemlab(process.stderr, "cohi_stemlab_live_upconverter",
                             line_callback=self._on_upconverter_stderr_line)

        self._live_upconverter_process = process
        logger.info("COHI-Stream (STEMlab): cohi_stemlab_live_upconverter started (PID %s, %d stations)",
                    process.pid, len(stations))
        self._raise_process_priority(self._live_upconverter_process)
        self._start_upconverter_health_check()

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

    def _start_upconverter_health_check(self):
        process = self._live_upconverter_process
        stop_event = threading.Event()
        self._upconverter_health_stop_event = stop_event

        def _run():
            while not stop_event.wait(timeout=0.5):
                returncode = process.poll()
                if returncode is None:
                    continue
                signal_detail = ""
                if returncode < 0:
                    try:
                        signal_detail = f" ({signal.Signals(-returncode).name})"
                    except ValueError:
                        signal_detail = f" (signal {-returncode})"
                logger.error(
                    "COHI-Stream (STEMlab): cohi_stemlab_live_upconverter (PID %s) "
                "terminated unexpectedly (return code %s%s)",
                    process.pid, returncode, signal_detail)
                self.errorOccurred.emit(tr(
                    "cohi_stemlab_live_upconverter terminated unexpectedly "
                    "(return code {code}{detail})").format(code=returncode, detail=signal_detail))
                return
        threading.Thread(target=_run, name="stemlab-upconverter-health-check", daemon=True).start()

    def _start_live_pump_thread(self):
        output_queue = _WavIQBuffer()
        self._live_pump_stop_event = threading.Event()
        self._live_pump_thread = threading.Thread(
            target=self._pump_read_loop,
            args=(self._live_upconverter_process, output_queue, self._live_pump_stop_event),
            name="stemlab-live-pump-read", daemon=True)
        self._live_pump_thread.start()
        self._network_send_thread = threading.Thread(
            target=self._network_send_loop,
            args=(output_queue, self.data_sock, self._live_pump_stop_event),
            name="stemlab-live-pump-send", daemon=True)
        self._network_send_thread.start()

    @staticmethod
    def _pump_read_loop(process, output_queue, stop_event):
        CHUNK_SIZE = 65536
        DIAG_LOG_INTERVAL_S = 10.0
        window_start = time.monotonic()
        read_time_total = 0.0
        send_time_total = 0.0
        send_time_max = 0.0
        iteration_count = 0
        try:
            while not stop_event.is_set():
                t_read_start = time.monotonic()
                chunk = process.stdout.read(CHUNK_SIZE)
                t_read_done = time.monotonic()
                if not chunk:
                    break
                output_queue.send(chunk)
                t_send_done = time.monotonic()

                read_time_total += t_read_done - t_read_start
                send_duration = t_send_done - t_read_done
                send_time_total += send_duration
                send_time_max = max(send_time_max, send_duration)
                iteration_count += 1

                now = t_send_done
                if now - window_start >= DIAG_LOG_INTERVAL_S:
                    read_avg_ms = (read_time_total / iteration_count * 1000) if iteration_count else 0.0
                    send_avg_ms = (send_time_total / iteration_count * 1000) if iteration_count else 0.0
                    logger.warning(
                        "[pump_read_diag] %d iterations in %.1fs, stdout.read() mean %.2fms, "
                    "output_queue.send() mean %.2fms, maximum %.2fms (blocks by design "
                    "when the queue is full)",
                        iteration_count, now - window_start, read_avg_ms, send_avg_ms, send_time_max * 1000)
                    window_start = now
                    read_time_total = 0.0
                    send_time_total = 0.0
                    send_time_max = 0.0
                    iteration_count = 0
        except (OSError, ValueError):
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

        for port in list(self._port_starved_streak.keys()):
            if port not in mentioned_ports:
                del self._port_starved_streak[port]

        stuck_ports = [port for port, streak in self._port_starved_streak.items()
                       if streak >= STUCK_CONSECUTIVE_WINDOWS]
        for port in stuck_ports:
            station = next((s for s in self._live_stations if s.udp_port == port), None)
            if station is not None:
                live_streams.restart_single_decoder(station)
            self._port_starved_streak.pop(port, None)

    def _stop_live_upconverter(self):
        if self._upconverter_health_stop_event is not None:
            self._upconverter_health_stop_event.set()
            self._upconverter_health_stop_event = None
        if self._live_pump_stop_event is not None:
            self._live_pump_stop_event.set()
        if self._live_upconverter_process is not None:
            self._live_upconverter_process.kill()
            try:
                self._live_upconverter_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._live_upconverter_process = None
        self._live_pump_thread = None
        self._network_send_thread = None

    def reload_live_stations(self, stations):
        if self._current_params is None:
            logger.warning("STEMlab: reload_live_stations() ignored without an active connection")
            return
        live_streams.stop_station_decoders(self._live_stations)
        live_streams.reassign_ports(stations)
        self._live_stations = stations
        if stations:
            live_streams.start_station_decoders(stations)
        self._stop_live_upconverter()
        iq_sample_rate, lo_freq = self._current_params
        self._spawn_live_upconverter(lo_freq, iq_sample_rate, self._live_stations)
        if self._live_upconverter_process is not None and self.program_variant == "COHI-Stream":
            self._start_live_pump_thread()

    def shutdown(self):
        self.infoMessage.emit(tr("shutting down STEMlab ..."))
        self.close()
        if not self._connect_ssh():
            return
        self._stop_server()
        _, stdout, _ = self._ssh.exec_command("/sbin/poweroff >&1 2>&1")
        channel = stdout.channel
        while channel.recv(1):
            pass
        self.infoMessage.emit(tr("STEMlab shut down"))

    def close(self):
        self._stop_combine_thread()
        self._stop_live_upconverter()
        if self._live_stations:
            live_streams.stop_station_decoders(self._live_stations)
        self._live_stations = []
        self._current_params = None
        sockets_to_close = [self.ctrl_sock, self.data_sock]
        if self._real_data_sock is not None:
            sockets_to_close.append(self._real_data_sock)
        for sock in sockets_to_close:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.ctrl_sock = None
        self.data_sock = None
        self._real_data_sock = None
