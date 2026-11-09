# Output backend for smiSDR / parlioSDR (radiolab81), the same network
# interface that COHIRADIAStreamer uses in its classic (non I/Q) mode:
#
#   control port (default 5000, TCP, OPTIONAL - not used if the target
#       hardware is already fixed to its rate/width): one short connection
#       per command
#       "width <8|16>\n"      DAC bus width
#       "rate <MSps, 1 decimal>\n"   e.g. "rate 5.0" / "rate 12.5"
#   data port    (default 1234, TCP): ONE long connection, raw RF samples
#       8 bit native   : int8   (optionally +128 = unsigned, "DC offset")
#       8 bit in 16 bit: int16 little endian, values in the 8 bit range
#
# The digital upconversion itself is NOT done here: the unchanged DSP core
# of this project (cohi_native_upconverter, the same tool as for FL2K) is
# started with --target-rate 5 MHz or 12.5 MHz. This backend only
# configures the device and forwards the upconverter output to the socket.

import logging
import os
import socket
import subprocess
import threading
import time

import numpy as np
import psutil

import live_streams
from fl2k_backend import _PipeSink
from fl2k_cpp_backend import Fl2kCppBackend
from i18n import tr

logger = logging.getLogger(__name__)

SMISDR_RATES_HZ = (5_000_000, 10_000_000, 12_500_000)
SMISDR_RATE_AUTO = "auto"
SMISDR_FORMAT_8 = 8
SMISDR_FORMAT_8_IN_16 = 816
SMISDR_FORMATS = (SMISDR_FORMAT_8, SMISDR_FORMAT_8_IN_16)

DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_DATA_PORT = 1234
DEFAULT_CONTROL_PORT = 5000

CONTROL_TIMEOUT_S = 0.5
REACHABLE_TIMEOUT_S = 2.0
DATA_CONNECT_TIMEOUT_S = 3.0
SETTLE_AFTER_CONTROL_S = 0.05

# A small kernel send buffer keeps the lag between player and DAC short
# (pause / track change); 512 KiB are ~40 ms at 12.5 MByte/s.
SEND_BUFFER_BYTES = 512 * 1024
PUMP_READ_BYTES = 64 * 1024

# Highest signal edge (centre frequency + sample rate / 2) that is allowed
# relative to the Nyquist frequency of the chosen output rate.
MAX_EDGE_OF_NYQUIST = 0.95


def parse_control_port(value, default=DEFAULT_CONTROL_PORT):
    """Port number, or None if the control port is switched off
    (0 / off / none / no / false; note: YAML reads an unquoted 'off' as False)."""
    if value is None:
        return default
    if value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "off", "none", "no", "false", "0"):
        return None
    port = int(value)
    if port == 0:
        return None
    if not 1 <= port <= 65535:
        raise ValueError("port out of range 1..65535")
    return port


def parse_rate_setting(value):
    """'auto' or 5 / 12.5 (MSps, also 5000000 / 12500000 Hz) -> 'auto' or Hz (int).
    Returns None for anything else."""
    if value is None:
        return SMISDR_RATE_AUTO
    if isinstance(value, str):
        text = value.strip().lower().replace(",", ".")
        if text == SMISDR_RATE_AUTO:
            return SMISDR_RATE_AUTO
        text = text.replace("msps", "").replace("mhz", "").strip()
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        return None
    hz = number * 1_000_000 if number < 1_000_000 else number
    for allowed in SMISDR_RATES_HZ:
        if abs(hz - allowed) < 1.0:
            return allowed
    return None


def format_rate_command(rate_hz):
    return "rate {:.1f}\n".format(rate_hz / 1_000_000.0)


def format_width_command(sample_format):
    return "width {}\n".format(8 if sample_format == SMISDR_FORMAT_8 else 16)


class SmisdrBackend(Fl2kCppBackend):
    """smiSDR / parlioSDR via TCP. Derives from Fl2kCppBackend to reuse the
    complete COHI-Stream handling (decoders, upconverter arguments, stderr
    monitoring); everything that is specific to FL2K (fl2k_file, USB) is
    replaced here."""

    default_gain = 0.4

    def __init__(self, address=DEFAULT_ADDRESS, data_port=DEFAULT_DATA_PORT,
                 control_port=DEFAULT_CONTROL_PORT, rate_setting=SMISDR_RATE_AUTO,
                 sample_format=SMISDR_FORMAT_8, offset_binary=False,
                 upconverter_tool_path=None, live_enabled=False,
                 live_stations_file=None, parent=None):
        super().__init__(fl2k_tool_path=None, upconverter_tool_path=upconverter_tool_path,
                         live_enabled=live_enabled, live_stations_file=live_stations_file,
                         parent=parent)
        self.address = address or DEFAULT_ADDRESS
        self.data_port = int(data_port)
        self.control_port = control_port  # None = no control port (fixed hardware)
        self.rate_setting = rate_setting
        self.sample_format = sample_format
        self.offset_binary = bool(offset_binary)
        self._net_sock = None
        self._pump_thread = None
        self._pump_stop_event = None
        self._pump_failed = False

    @staticmethod
    def identify():
        return {
            "rates": {"values": list(SMISDR_RATES_HZ)},
            "rate_type": "discrete",
            "RX": False,
            "TX": True,
            "device_name": "smisdr",
            "connection_type": "TCP/IP (smiSDR / parlioSDR, control port + data port, "
                               "upconversion in C++)",
        }

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect_device(self, configured_address):
        logger.info("smisdr: connect_device started (%s, control port %s, data port %d)",
                    self.address, self.control_port or "off", self.data_port)
        self._kill_orphan_processes()

        if not self._upconverter_binary_available():
            logger.error("smisdr: cohi_native_upconverter not found: %s",
                         self.upconverter_tool_path)
            self.errorOccurred.emit(tr(
                "cohi_native_upconverter not found: {path} "
                "(see cohi_native_upconverter.cpp - must be compiled first, "
                "e.g. with 'make', or set fl2k_cpp_upconverter_path in config_sdr.yaml)"
            ).format(path=self.upconverter_tool_path))
            return None

        if self.control_port is not None:
            try:
                probe = socket.create_connection((self.address, self.control_port),
                                                 timeout=REACHABLE_TIMEOUT_S)
                probe.close()
            except OSError as exc:
                logger.error("smisdr: control port %s:%d not reachable: %s",
                             self.address, self.control_port, exc)
                self.errorOccurred.emit(tr(
                    "smiSDR/parlioSDR not reachable at {address}:{port} ({error})"
                ).format(address=self.address, port=self.control_port, error=exc))
                return None
        else:
            logger.info("smisdr: no control port configured - width/rate are not sent, "
                        "the hardware must already be set to %s", self.rate_setting)

        if self.live_enabled:
            self._start_live_streams()

        logger.info("smisdr: connect_device finished")
        return "{}:{}".format(self.address, self.data_port)

    def _send_control_command(self, command):
        try:
            with socket.create_connection((self.address, self.control_port),
                                          timeout=CONTROL_TIMEOUT_S) as ctrl:
                ctrl.settimeout(CONTROL_TIMEOUT_S)
                ctrl.sendall(command.encode("ascii"))
        except OSError as exc:
            logger.error("smisdr: control command %r failed: %s", command.strip(), exc)
            return False
        logger.info("smisdr: control command sent: %s", command.strip())
        return True

    # ------------------------------------------------------------------
    # rate selection
    # ------------------------------------------------------------------
    def _compute_upconverter_params(self, sample_rate, center_freq):
        """Output rate: exactly 5 or 12.5 MSps. Returns (rate_hz, None) or
        (None, None) if the recording does not fit below Nyquist."""
        upper_edge = center_freq + sample_rate / 2.0
        if self.rate_setting == SMISDR_RATE_AUTO and self.control_port is not None:
            candidates = SMISDR_RATES_HZ
        elif self.rate_setting == SMISDR_RATE_AUTO:
            return None, None  # without control port the rate must be given explicitly
        else:
            candidates = (self.rate_setting,)
        for rate in candidates:
            if upper_edge <= MAX_EDGE_OF_NYQUIST * rate / 2.0:
                return rate, None
        return None, None

    # ------------------------------------------------------------------
    # pipeline
    # ------------------------------------------------------------------
    def _pipeline_alive(self):
        return (self._upconverter_process is not None
                and self._upconverter_process.poll() is None
                and self._net_sock is not None and not self._pump_failed)

    def configure_track(self, sample_rate, center_freq, force=False):
        if not force and self._pipeline_alive() and self._current_params == (sample_rate, center_freq):
            logger.info("smisdr: same frequency/sample rate as the active pipeline - "
                        "no reconfiguration needed")
            self.infoMessage.emit(tr("output pipeline kept unchanged"))
            return True

        logger.info("smisdr: configure_track started (sample_rate=%s, center_freq=%s)",
                    sample_rate, center_freq)
        self._stop_processes()

        target_rate, _unused = self._compute_upconverter_params(sample_rate, center_freq)
        if target_rate is None:
            upper_edge = center_freq + sample_rate / 2.0
            logger.error("smisdr: signal up to %.0f Hz does not fit below Nyquist for "
                         "smisdr_sample_rate=%s", upper_edge, self.rate_setting)
            self.errorOccurred.emit(tr(
                "recording (up to {edge} Hz) does not fit the smisdr output rate "
                "({setting}) - use a higher smisdr_sample_rate"
            ).format(edge=int(upper_edge), setting=self.rate_setting))
            return False
        self.target_rate = target_rate
        logger.info("smisdr: output sample rate: %s Hz", target_rate)

        if self.control_port is not None:
            if not (self._send_control_command(format_width_command(self.sample_format))
                    and self._send_control_command(format_rate_command(target_rate))):
                self.errorOccurred.emit(tr(
                    "smiSDR/parlioSDR control port {address}:{port} did not accept the "
                    "width/rate command"
                ).format(address=self.address, port=self.control_port))
                return False
            time.sleep(SETTLE_AFTER_CONTROL_S)

        try:
            sock = socket.create_connection((self.address, self.data_port),
                                            timeout=DATA_CONNECT_TIMEOUT_S)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
        except OSError as exc:
            logger.error("smisdr: data port %s:%d not reachable: %s",
                         self.address, self.data_port, exc)
            self.errorOccurred.emit(tr(
                "smiSDR/parlioSDR data port {address}:{port} not reachable ({error})"
            ).format(address=self.address, port=self.data_port, error=exc))
            return False
        self._net_sock = sock

        try:
            self._upconverter_process = self._spawn_upconverter_process(
                sample_rate, center_freq, target_rate)
            logger.info("smisdr: cohi_native_upconverter started (PID %s)",
                        self._upconverter_process.pid)
        except OSError as exc:
            logger.error("smisdr: cohi_native_upconverter could not be started: %s", exc)
            self.errorOccurred.emit(tr("cohi_native_upconverter could not be started: {error}").format(
                error=exc))
            self._stop_processes()
            return False

        self._raise_process_priority(self._upconverter_process)

        self._pump_failed = False
        self._pump_stop_event = threading.Event()
        self._pump_thread = threading.Thread(
            target=self._pump_loop,
            args=(self._upconverter_process, sock, self._pump_stop_event),
            name="smisdr-pump", daemon=True)
        self._pump_thread.start()

        self.data_sock = _PipeSink(self._upconverter_process)
        self._current_params = (sample_rate, center_freq)
        self._start_upconverter_health_check()
        self.infoMessage.emit(tr(
            "smisdr output started (IF {if_hz} Hz, {rate} MSps, {fmt})"
        ).format(if_hz=center_freq, rate="{:g}".format(target_rate / 1e6),
                 fmt=self._format_label()))
        return True

    def _format_label(self):
        if self.sample_format == SMISDR_FORMAT_8_IN_16:
            return "8 bit in 16 bit"
        return "8 bit unsigned" if self.offset_binary else "8 bit"

    def _convert_block(self, raw):
        """int8 output of the upconverter -> wire format of the DAC."""
        if self.sample_format == SMISDR_FORMAT_8_IN_16:
            return np.frombuffer(raw, dtype=np.int8).astype("<i2").tobytes()
        if self.offset_binary:
            return np.bitwise_xor(np.frombuffer(raw, dtype=np.uint8), 0x80).tobytes()
        return raw

    def _pump_loop(self, process, sock, stop_event):
        """upconverter stdout -> TCP data connection."""
        passthrough = (self.sample_format == SMISDR_FORMAT_8 and not self.offset_binary)
        stdout_fd = process.stdout.fileno()
        try:
            while not stop_event.is_set():
                raw = os.read(stdout_fd, PUMP_READ_BYTES)
                if not raw:
                    break
                sock.sendall(raw if passthrough else self._convert_block(raw))
        except (OSError, ValueError) as exc:
            if not stop_event.is_set():
                logger.error("smisdr: sending to %s:%d failed: %s",
                             self.address, self.data_port, exc)
                self._pump_failed = True
                if process.poll() is None:
                    process.terminate()
                self.errorOccurred.emit(tr("smisdr connection lost: {error}").format(error=exc))
            return
        if not stop_event.is_set():
            self._pump_failed = True

    def _start_upconverter_health_check(self):
        process = self._upconverter_process
        stop_event = threading.Event()
        self._upconverter_health_stop_event = stop_event

        def _run():
            while not stop_event.wait(timeout=0.5):
                returncode = process.poll()
                if returncode is None:
                    continue
                already_reported = self._pump_failed
                self._pump_failed = True
                if already_reported:
                    return
                logger.error("smisdr: cohi_native_upconverter (PID %s) terminated "
                             "unexpectedly (return code %s)", process.pid, returncode)
                if not stop_event.is_set():
                    self.errorOccurred.emit(tr(
                        "cohi_native_upconverter terminated unexpectedly "
                        "(return code {code}{detail})").format(code=returncode, detail=""))
                return
        threading.Thread(target=_run, name="smisdr-health-check", daemon=True).start()

    def _stop_processes(self):
        if self._upconverter_health_stop_event is not None:
            self._upconverter_health_stop_event.set()
            self._upconverter_health_stop_event = None
        if self._pump_stop_event is not None:
            self._pump_stop_event.set()

        if self._net_sock is not None:
            try:
                self._net_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._net_sock.close()
            except OSError:
                pass
            self._net_sock = None

        process = self._upconverter_process
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass

        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2)
            self._pump_thread = None
        self._pump_stop_event = None

        if process is not None:
            try:
                if process.stdout:
                    process.stdout.close()
            except OSError:
                pass

        self._upconverter_process = None
        self._fl2k_process = None
        self.data_sock = None
        self._current_params = None
        self._pump_failed = False

    def close(self):
        live_streams.stop_station_decoders(self._live_stations)
        self._live_stations = []
        self.infoMessage.emit(tr("stopping cohi_native_upconverter ..."))
        self._stop_processes()
        self.infoMessage.emit(tr("smisdr output stopped"))

    @staticmethod
    def _kill_orphan_processes():
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "cohi_native_upconverter" in name or "cohi_native_upconverter" in cmdline:
                logger.info("terminating orphaned process: %s (PID %s)", name, proc.info.get("pid"))
                try:
                    proc.kill()
                except psutil.Error:
                    pass
