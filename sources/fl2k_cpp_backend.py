####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
import re
import shutil
import signal
import subprocess
import threading

import psutil

import live_streams
from i18n import tr
from fl2k_backend import IS_WINDOWS, PIPE_BUFSIZE, Fl2kBackend, _PipeSink, _drain_pipe

logger = logging.getLogger(__name__)

DEFAULT_UPCONVERTER_PATH = os.path.join(
    os.getcwd(), "cohi_native_upconverter.exe" if IS_WINDOWS else "cohi_native_upconverter")

OUTPUT_SHIFT = 6

FIR_TAPS = 15

LIVE_GAIN_SHIFT = 2

STARVED_FRACTION_THRESHOLD = 0.8
STARVED_SAMPLES_PER_WINDOW_APPROX = 220500
STARVED_COUNT_THRESHOLD = int(STARVED_SAMPLES_PER_WINDOW_APPROX * STARVED_FRACTION_THRESHOLD)
STUCK_CONSECUTIVE_WINDOWS = 3

_AUSLASTUNG_LINE_RE = re.compile(
    r"hungrig gesamt: \d+, betroffene Ports \(Port:Anzahl\): (.*?), "
    r"(?:Live-Kanaele )?Ueberlauf gesamt:")


class Fl2kCppBackend(Fl2kBackend):
    default_gain = 0.4

    def __init__(self, fl2k_tool_path=None, upconverter_tool_path=None,
                 live_enabled=False, live_stations_file=None, parent=None):
        super().__init__(fl2k_tool_path=fl2k_tool_path, ffmpeg_tool_path=None, parent=parent)
        self.upconverter_tool_path = upconverter_tool_path or DEFAULT_UPCONVERTER_PATH
        self._upconverter_process = None
        self._upconverter_health_stop_event = None
        self.debug_udp_port = 0
        self.target_rate = None
        self.live_enabled = live_enabled
        self.live_stations_file = live_stations_file
        self._live_stations = []
        self._port_starved_streak = {}

    @property
    def silence_required_during_mute(self):
        return not self._live_stations

    @staticmethod
    def identify():
        info = Fl2kBackend.identify()
        info["connection_type"] = "USB (local, driven via fl2k_file, upconversion in C++)"
        return info

    def set_debug_udp_port(self, port):
        self.debug_udp_port = port

    def _upconverter_binary_available(self):
        command = self.upconverter_tool_path
        if os.path.isabs(command) or os.sep in command:
            return os.path.isfile(command)
        return shutil.which(command) is not None

    def connect_device(self, configured_address):
        result = super().connect_device(configured_address)
        if result is None:
            return None
        if not self._upconverter_binary_available():
            logger.error("fl2k (cpp): cohi_native_upconverter not found: %s",
                          self.upconverter_tool_path)
            self.errorOccurred.emit(tr(
                "cohi_native_upconverter not found: {path} "
                "(see cohi_native_upconverter.cpp - must be compiled first, "
                "e.g. with 'make', or set fl2k_cpp_upconverter_path in config_sdr.yaml)"
            ).format(path=self.upconverter_tool_path))
            return None
        if self.live_enabled:
            self._start_live_streams()
        return result

    def _start_live_streams(self):
        if not self.live_stations_file:
            logger.warning("fl2k (cpp): COHI-Stream enabled but no station list "
                "configured (live_stations_file) - COHI-Stream stays without stations")
            return
        self._live_stations = live_streams.load_station_list(self.live_stations_file)
        if not self._live_stations:
            logger.warning("fl2k (cpp): COHI-Stream enabled but no valid stations "
                "found in %s", self.live_stations_file)
            return
        live_streams.start_station_decoders(self._live_stations)
        self.infoMessage.emit(tr("COHI-Stream: {count} stations started").format(count=len(self._live_stations)))

    def close(self):
        live_streams.stop_station_decoders(self._live_stations)
        self._live_stations = []
        super().close()

    def _pipeline_alive(self):
        return (self._upconverter_process is not None and self._upconverter_process.poll() is None
                and self._fl2k_process is not None and self._fl2k_process.poll() is None)

    def configure_track(self, sample_rate, center_freq, force=False):
        if not force and self._pipeline_alive() and self._current_params == (sample_rate, center_freq):
            logger.info("fl2k (cpp): same frequency/sample rate as the active pipeline - "
                "no reconfiguration needed")
            self.infoMessage.emit(tr("output pipeline kept unchanged"))
            return True

        logger.info("fl2k (cpp): configure_track started (sample_rate=%s, center_freq=%s)",
                     sample_rate, center_freq)
        self._stop_processes()
        logger.info("fl2k (cpp): old processes (if any) stopped")

        target_rate, _unused_allpass_coeff = self._compute_upconverter_params(sample_rate, center_freq)
        logger.info("fl2k (cpp): target sample rate calculated: target_rate=%s", target_rate)
        self.target_rate = target_rate

        try:
            logger.info("fl2k (cpp): starting cohi_native_upconverter process ...")
            self._upconverter_process = self._spawn_upconverter_process(
                sample_rate, center_freq, target_rate)
            logger.info("fl2k (cpp): cohi_native_upconverter started (PID %s)",
                         self._upconverter_process.pid)

            logger.info("fl2k (cpp): starting fl2k_file process ...")
            self._fl2k_process = self._spawn_fl2k_process(
                target_rate, stdin=self._upconverter_process.stdout)
            logger.info("fl2k (cpp): fl2k_file started (PID %s)", self._fl2k_process.pid)
        except OSError as exc:
            logger.error("fl2k (cpp): processes could not be started: %s", exc)
            self.errorOccurred.emit(tr("cohi_native_upconverter/fl2k_file could not be started: {error}").format(
                error=exc))
            self._stop_processes()
            return False

        self._raise_process_priority(self._upconverter_process)
        self._raise_process_priority(self._fl2k_process)

        self.data_sock = _PipeSink(self._upconverter_process)
        self._current_params = (sample_rate, center_freq)
        self._start_upconverter_health_check()
        logger.info("fl2k (cpp): configure_track finished, data_sock ready")
        self.infoMessage.emit(tr("fl2k upconverter (cpp) started (IF {if_hz} Hz, target sample rate {rate} Hz)").format(
            if_hz=center_freq, rate=target_rate))
        return True

    def reload_live_stations(self, stations):
        if self._current_params is None:
            logger.warning("fl2k (cpp): reload_live_stations() called before the first "
                "configure_track() run - ignored")
            return

        logger.info("fl2k (cpp): station list is being updated (%d stations)", len(stations))
        live_streams.stop_station_decoders(self._live_stations)
        live_streams.reassign_ports(stations)
        self._live_stations = stations
        live_streams.start_station_decoders(self._live_stations)

        sample_rate, center_freq = self._current_params
        self.configure_track(sample_rate, center_freq, force=True)

    def _start_upconverter_health_check(self):
        process = self._upconverter_process
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
                    "fl2k (cpp): cohi_native_upconverter (PID %s) terminated unexpectedly "
                "(return code %s%s) - stopping fl2k_file to avoid the otherwise endless "
                "repeat loop",
                    process.pid, returncode, signal_detail)
                self.errorOccurred.emit(tr(
                    "cohi_native_upconverter terminated unexpectedly "
                    "(return code {code}{detail})").format(code=returncode, detail=signal_detail))
                fl2k_proc = self._fl2k_process
                if fl2k_proc is not None and fl2k_proc.poll() is None:
                    fl2k_proc.kill()
                return
        threading.Thread(target=_run, name="upconverter-health-check", daemon=True).start()

    def _spawn_upconverter_process(self, sample_rate, center_freq, target_rate):
        command = [
            self.upconverter_tool_path,
            "--sample-rate", str(sample_rate),
            "--center-freq", str(center_freq),
            "--target-rate", str(target_rate),
            "--output-shift", str(OUTPUT_SHIFT),
            "--fir-taps", str(FIR_TAPS),
        ]
        if self._live_stations:
            command += live_streams.build_live_channel_args(self._live_stations)
            command += ["--live-gain-shift", str(LIVE_GAIN_SHIFT)]
        if self.debug_udp_port:
            command += ["--debug-udp-port", str(self.debug_udp_port)]
        logger.info("fl2k (cpp): cohi_native_upconverter command: %s", " ".join(command))
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=PIPE_BUFSIZE)
        logger.info("fl2k (cpp): cohi_native_upconverter Popen returned (PID %s), "
                "starting pipe drain for stderr", process.pid)
        _drain_pipe(process.stderr, "cohi_native_upconverter",
                    line_callback=self._on_upconverter_stderr_line)
        return process

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

    def _stop_processes(self):
        if self._upconverter_health_stop_event is not None:
            self._upconverter_health_stop_event.set()
            self._upconverter_health_stop_event = None

        if self._fl2k_process is not None and self._fl2k_process.poll() is None:
            self._fl2k_process.kill()
            try:
                self._fl2k_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        if self._upconverter_process is not None:
            try:
                if self._upconverter_process.stdin:
                    self._upconverter_process.stdin.close()
                if self._upconverter_process.stdout:
                    self._upconverter_process.stdout.close()
            except OSError:
                pass
            if self._upconverter_process.poll() is None:
                self._upconverter_process.terminate()
                try:
                    self._upconverter_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._upconverter_process.kill()

        self._upconverter_process = None
        self._fl2k_process = None
        self.data_sock = None
        self._current_params = None

    @staticmethod
    def _kill_orphan_processes():
        Fl2kBackend._kill_orphan_processes()
        logger.info("fl2k (cpp): scanning running processes for orphaned "
                "cohi_native_upconverter ...")
        scanned = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            scanned += 1
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
        logger.info("fl2k (cpp): %d running processes scanned", scanned)
