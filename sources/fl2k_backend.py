####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
import shutil
import subprocess
import threading
import time

import numpy as np
import psutil

from device_backend import DeviceBackend
from i18n import tr

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name != "posix"

DEFAULT_FL2K_FILE_PATH = os.path.join(
    os.getcwd(), "dev_drivers", "fl2k", "osmo-fl2k-64bit-20250105", "fl2k_file.exe")
DEFAULT_FFMPEG_PATH = os.path.join(
    os.getcwd(), "ffmpeg-master-latest-win64-gpl-shared", "bin", "ffmpeg.exe")

TARGET_RATE_STEP = 10_000_000
MAX_TARGET_SAMPLE_RATE = 100_000_000


PRESET_VOLUME = 256 * 2

DRAIN_LOG_WINDOW_S = 2.0
DRAIN_LOG_BURST_THRESHOLD = 20

PIPE_BUFSIZE = 0


def _drain_pipe(pipe, label, line_callback=None):
    def _run():
        window_start = time.monotonic()
        window_count = 0
        suppressed_count = 0
        last_suppressed_line = None

        def _flush_suppressed():
            if suppressed_count:
                logger.warning("[%s] ... %d more messages suppressed within %.0fs "
                    "(last example: %s)",
                                label, suppressed_count, DRAIN_LOG_WINDOW_S, last_suppressed_line)

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
                if now - window_start >= DRAIN_LOG_WINDOW_S:
                    _flush_suppressed()
                    window_start = now
                    window_count = 0
                    suppressed_count = 0

                window_count += 1
                if window_count <= DRAIN_LOG_BURST_THRESHOLD:
                    logger.warning("[%s] %s", label, text)
                else:
                    suppressed_count += 1
                    last_suppressed_line = text
        except (OSError, ValueError):
            pass
        finally:
            _flush_suppressed()

    thread = threading.Thread(target=_run, name=f"drain-{label}", daemon=True)
    thread.start()
    return thread


class _PipeSink:
    _HEARTBEAT_INTERVAL_S = 60

    def __init__(self, process):
        self._process = process
        self._bytes_since_heartbeat = 0
        self._last_heartbeat = time.monotonic()

    def send(self, data):
        payload = data.tobytes() if hasattr(data, "tobytes") else data
        self._process.stdin.write(payload)
        self._process.stdin.flush()

        self._bytes_since_heartbeat += len(payload)
        now = time.monotonic()
        if now - self._last_heartbeat >= self._HEARTBEAT_INTERVAL_S:
            logger.info("fl2k: heartbeat - %d bytes sent to ffmpeg (PID %s) in the last %.0fs",
                        self._bytes_since_heartbeat, now - self._last_heartbeat, self._process.pid)
            self._bytes_since_heartbeat = 0
            self._last_heartbeat = now


class Fl2kBackend(DeviceBackend):
    default_gain = 0.045

    def __init__(self, fl2k_tool_path=None, ffmpeg_tool_path=None, parent=None):
        super().__init__(parent)
        self.fl2k_tool_path = fl2k_tool_path or (
            DEFAULT_FL2K_FILE_PATH if IS_WINDOWS else "fl2k_file")
        self.ffmpeg_tool_path = ffmpeg_tool_path or (
            DEFAULT_FFMPEG_PATH if IS_WINDOWS else "ffmpeg")
        self._ffmpeg_process = None
        self._fl2k_process = None
        self.data_sock = None
        self._current_params = None

    @staticmethod
    def identify():
        return {
            "rates": {"min": 1_000, "max": 5_000_000},
            "rate_type": "continuous",
            "RX": False,
            "TX": True,
            "device_name": "fl2k",
            "connection_type": "USB (local, driven via ffmpeg/fl2k_file)",
        }

    def connect_device(self, configured_address):
        logger.info("fl2k: connect_device started")
        self._kill_orphan_processes()
        logger.info("fl2k: orphaned processes cleaned up")

        if not self._fl2k_binary_available():
            logger.error("fl2k: fl2k_file not found: %s", self._fl2k_binary_command())
            self.errorOccurred.emit(tr("fl2k_file not found: {path}").format(path=self._fl2k_binary_command()))
            return None

        logger.info("fl2k: connect_device finished")
        return "fl2k (lokal)"

    def _fl2k_binary_command(self):
        return self.fl2k_tool_path

    def _fl2k_binary_available(self):
        command = self.fl2k_tool_path
        if os.path.isabs(command) or os.sep in command:
            return os.path.isfile(command)
        return shutil.which(command) is not None

    def configure_track(self, sample_rate, center_freq):
        if self._pipeline_alive() and self._current_params == (sample_rate, center_freq):
            logger.info("fl2k: same frequency/sample rate as the active pipeline - no reconfiguration needed")
            self.infoMessage.emit(tr("output pipeline kept unchanged"))
            return True

        logger.info("fl2k: configure_track started (sample_rate=%s, center_freq=%s)",
                     sample_rate, center_freq)
        self._stop_processes()
        logger.info("fl2k: old processes (if any) stopped")

        target_rate, allpass_coeff = self._compute_upconverter_params(sample_rate, center_freq)
        logger.info("fl2k: upconverter parameters calculated: target_rate=%s, allpass_coeff=%s",
                     target_rate, allpass_coeff)
        self._warn_if_unstable_ratio(target_rate, sample_rate)

        try:
            logger.info("fl2k: starting ffmpeg process ...")
            self._ffmpeg_process = self._spawn_ffmpeg_process(
                sample_rate, center_freq, target_rate, allpass_coeff)
            logger.info("fl2k: ffmpeg started (PID %s)", self._ffmpeg_process.pid)

            logger.info("fl2k: starting fl2k_file process ...")
            self._fl2k_process = self._spawn_fl2k_process(
                target_rate, stdin=self._ffmpeg_process.stdout)
            logger.info("fl2k: fl2k_file started (PID %s)", self._fl2k_process.pid)
        except OSError as exc:
            logger.error("fl2k: ffmpeg/fl2k_file could not be started: %s", exc)
            self.errorOccurred.emit(tr("ffmpeg/fl2k_file could not be started: {error}").format(error=exc))
            self._stop_processes()
            return False

        logger.info("fl2k: setting process priority ...")
        self._raise_process_priority(self._ffmpeg_process)
        self._raise_process_priority(self._fl2k_process)
        logger.info("fl2k: process priority set")

        self.data_sock = _PipeSink(self._ffmpeg_process)
        self._current_params = (sample_rate, center_freq)
        logger.info("fl2k: configure_track finished, data_sock ready")
        self.infoMessage.emit(tr("fl2k upconverter started (IF {if_hz} Hz, target sample rate {rate} Hz)").format(
            if_hz=center_freq, rate=target_rate))
        return True

    def _pipeline_alive(self):
        return (self._ffmpeg_process is not None and self._ffmpeg_process.poll() is None
                and self._fl2k_process is not None and self._fl2k_process.poll() is None)

    @staticmethod
    def _raise_process_priority(process):
        try:
            proc = psutil.Process(process.pid)
            if IS_WINDOWS:
                proc.nice(psutil.HIGH_PRIORITY_CLASS)
            else:
                proc.nice(-10)
        except psutil.Error as exc:
            logger.warning("process priority could not be set: %s", exc)

    def _compute_upconverter_params(self, sample_rate, center_freq):
        target_rate = TARGET_RATE_STEP * (1 + np.floor(
            (center_freq + sample_rate / 2) * 2 / TARGET_RATE_STEP))
        target_rate = int(min(MAX_TARGET_SAMPLE_RATE, target_rate))

        tan_term = np.tan(np.pi * center_freq / target_rate)
        allpass_coeff = (tan_term - 1) / (tan_term + 1)
        return target_rate, allpass_coeff

    def _warn_if_unstable_ratio(self, target_rate, sample_rate):
        stable = (np.mod(np.log2(target_rate / sample_rate), 1) == 0) or sample_rate < 500_001
        if not stable:
            self.infoMessage.emit(tr(
                "ratio of target to source sample rate ({ratio:.2f}) is not a power of two - "
                "may cause audio problems.").format(ratio=target_rate / sample_rate))

    def _spawn_ffmpeg_process(self, sample_rate, center_freq, target_rate, allpass_coeff):
        filter_complex = (
            f"[0:a]aresample=osr={target_rate},channelsplit=channel_layout=stereo [re][im];"
            f"sine=frequency={center_freq}:sample_rate={target_rate}[sine_base];"
            "[sine_base] asplit=2[sine_sin1][sine_sin2];"
            f"[sine_sin2]biquad=b0={allpass_coeff}:b1=1:b2=0:a0=1:a1={allpass_coeff}:a2=0[sine_cos];"
            "[re][sine_cos]amultiply[mod_re];"
            "[im][sine_sin1]amultiply[mod_im];"
            f"[mod_im]volume=volume={PRESET_VOLUME}[part_im];"
            f"[mod_re]volume=volume={PRESET_VOLUME}[part_re];"
            "[part_re][part_im]amix=inputs=2:duration=shortest[out]"
        )
        ffmpeg_binary = self.ffmpeg_tool_path
        command = [
            ffmpeg_binary, "-y", "-loglevel", "error", "-hide_banner",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "2", "-i", "-",
            "-filter_complex", filter_complex,
            "-map", "[out]", "-c:a", "pcm_s8", "-f", "s8", "-",
        ]
        logger.info("fl2k: ffmpeg command: %s", " ".join(command))
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=PIPE_BUFSIZE)
        logger.info("fl2k: ffmpeg Popen returned (PID %s), starting pipe drain for stderr", process.pid)
        _drain_pipe(process.stderr, "ffmpeg")
        return process

    def _spawn_fl2k_process(self, target_rate, stdin):
        fl2k_binary = self._fl2k_binary_command()
        command = [fl2k_binary, "-s", str(target_rate), "-r", "1", "-"]
        logger.info("fl2k: fl2k_file command: %s", " ".join(command))
        process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=PIPE_BUFSIZE)
        logger.info("fl2k: fl2k_file Popen returned (PID %s), starting pipe drain for stdout/stderr",
                     process.pid)
        _drain_pipe(process.stderr, "fl2k_file")
        _drain_pipe(process.stdout, "fl2k_file")
        return process

    def encode_samples(self, samples, gain):
        scaled = gain * samples.astype(np.float32)
        return np.clip(scaled, -32768, 32767).astype(np.int16)

    def shutdown(self):
        self.close()

    def close(self):
        self.infoMessage.emit(tr("stopping ffmpeg/fl2k_file ..."))
        self._stop_processes()
        self.infoMessage.emit(tr("fl2k pipeline stopped"))

    def _stop_processes(self):
        if self._fl2k_process is not None and self._fl2k_process.poll() is None:
            self._fl2k_process.kill()
            try:
                self._fl2k_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        if self._ffmpeg_process is not None:
            try:
                if self._ffmpeg_process.stdin:
                    self._ffmpeg_process.stdin.close()
                if self._ffmpeg_process.stdout:
                    self._ffmpeg_process.stdout.close()
            except OSError:
                pass
            if self._ffmpeg_process.poll() is None:
                self._ffmpeg_process.terminate()
                try:
                    self._ffmpeg_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._ffmpeg_process.kill()

        self._ffmpeg_process = None
        self._fl2k_process = None
        self.data_sock = None
        self._current_params = None

    @staticmethod
    def _kill_orphan_processes():
        logger.info("fl2k: scanning running processes for orphaned ffmpeg/fl2k_file ...")
        scanned = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            scanned += 1
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "fl2k_file" in name or "fl2k_file" in cmdline or "ffmpeg" in name or "ffmpeg" in cmdline:
                logger.info("terminating orphaned process: %s (PID %s)", name, proc.info.get("pid"))
                try:
                    proc.kill()
                except psutil.Error:
                    pass
        logger.info("fl2k: %d running processes scanned", scanned)
