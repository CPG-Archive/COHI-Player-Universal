####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import subprocess
import time

import numpy as np

from fl2k_backend import IS_WINDOWS, Fl2kBackend, _PipeSink
from i18n import tr

logger = logging.getLogger(__name__)

DSP_HEARTBEAT_INTERVAL_S = 10.0


class Fl2kNativeBackend(Fl2kBackend):
    default_gain = 0.005

    def __init__(self, fl2k_tool_path=None, parent=None):
        super().__init__(fl2k_tool_path=fl2k_tool_path, ffmpeg_tool_path=None, parent=parent)
        self._sample_rate = None
        self._upsample_factor = None
        self._interp_x_target = None
        self._interp_x_orig = None
        self._phase_ramp = None
        self._nco_phase = np.float32(0.0)
        self._phase_increment = np.float32(0.0)
        self._dsp_busy_s_since_heartbeat = 0.0
        self._dsp_available_s_since_heartbeat = 0.0
        self._dsp_heartbeat_at = None

    @staticmethod
    def identify():
        info = Fl2kBackend.identify()
        info["connection_type"] = "USB (local, driven via fl2k_file, upconversion in Python)"
        return info

    def _pipeline_alive(self):
        return self._fl2k_process is not None and self._fl2k_process.poll() is None

    def configure_track(self, sample_rate, center_freq):
        if self._pipeline_alive() and self._current_params == (sample_rate, center_freq):
            logger.info("fl2k (native): same frequency/sample rate as the active pipeline - "
                "no reconfiguration needed")
            self.infoMessage.emit(tr("output pipeline kept unchanged"))
            return True

        logger.info("fl2k (native): configure_track started (sample_rate=%s, center_freq=%s)",
                     sample_rate, center_freq)
        self._stop_processes()
        logger.info("fl2k (native): old processes (if any) stopped")

        target_rate, _unused_allpass_coeff = self._compute_upconverter_params(sample_rate, center_freq)
        logger.info("fl2k (native): target sample rate calculated: target_rate=%s", target_rate)

        if target_rate % sample_rate != 0:
            logger.error("fl2k (native): target sample rate %s Hz is not an integer multiple "
                         "of the recording sample rate %s Hz - this path currently only supports "
                         "integer ratios (see _setup_dsp_state). Please use fl2k_upconverter: "
                         "ffmpeg or report it so it can be extended.", target_rate, sample_rate)
            self.errorOccurred.emit(tr(
                "fl2k (native): target sample rate {target} Hz is not an integer multiple "
                "of the recording sample rate {source} Hz - this path currently only supports "
                "integer ratios. Please use fl2k_upconverter: ffmpeg or report it so it can "
                "be extended.").format(target=target_rate, source=sample_rate))
            return False

        self._setup_dsp_state(sample_rate, center_freq, target_rate,
                               upsample_factor=target_rate // sample_rate)

        try:
            logger.info("fl2k (native): starting fl2k_file process ...")
            self._fl2k_process = self._spawn_fl2k_process(target_rate, stdin=subprocess.PIPE)
            logger.info("fl2k (native): fl2k_file started (PID %s)", self._fl2k_process.pid)
        except OSError as exc:
            logger.error("fl2k (native): fl2k_file could not be started: %s", exc)
            self.errorOccurred.emit(tr("fl2k_file could not be started: {error}").format(error=exc))
            self._stop_processes()
            return False

        if IS_WINDOWS:
            self._raise_process_priority(self._fl2k_process)

        self.data_sock = _PipeSink(self._fl2k_process)
        self._current_params = (sample_rate, center_freq)
        logger.info("fl2k (native): configure_track finished, data_sock ready")
        self.infoMessage.emit(tr(
            "fl2k upconverter (native) started (IF {if_hz} Hz, target sample rate {rate} Hz, "
            "upsampling {factor}x)").format(if_hz=center_freq, rate=target_rate,
                                            factor=self._upsample_factor))
        return True

    def _setup_dsp_state(self, sample_rate, center_freq, target_rate, upsample_factor):
        self._sample_rate = sample_rate
        self._upsample_factor = upsample_factor

        self._interp_x_target = None
        self._interp_x_orig = None
        self._phase_ramp = None

        self._nco_phase = np.float32(0.0)
        self._phase_increment = np.float32(2.0 * np.pi * center_freq / target_rate)

        self._dsp_busy_s_since_heartbeat = 0.0
        self._dsp_available_s_since_heartbeat = 0.0
        self._dsp_heartbeat_at = None

    def encode_samples(self, samples, gain):
        started_at = time.monotonic()

        i_raw = samples[0::2].astype(np.float64)
        q_raw = samples[1::2].astype(np.float64)
        n_in = len(i_raw)
        if n_in == 0:
            return np.empty(0, dtype=np.int8)

        factor = self._upsample_factor
        n_up = n_in * factor

        if self._interp_x_target is None or len(self._interp_x_target) != n_up:
            self._interp_x_target = np.arange(n_up, dtype=np.float64) / factor
            self._interp_x_orig = np.arange(n_in, dtype=np.float64)
            self._phase_ramp = self._phase_increment * np.arange(n_up, dtype=np.float32)

        i_filt = np.interp(self._interp_x_target, self._interp_x_orig, i_raw)
        q_filt = np.interp(self._interp_x_target, self._interp_x_orig, q_raw)

        phase = self._nco_phase + self._phase_ramp
        self._nco_phase = np.float32(
            (self._nco_phase + self._phase_increment * n_up) % np.float32(2.0 * np.pi))

        mixed = i_filt.astype(np.float32) * np.cos(phase) - q_filt.astype(np.float32) * np.sin(phase)

        scaled = mixed * np.float32(gain)
        result = np.clip(scaled, -127, 127).astype(np.int8)

        elapsed_s = time.monotonic() - started_at
        available_s = n_in / self._sample_rate if self._sample_rate else 0.0
        self._dsp_busy_s_since_heartbeat += elapsed_s
        self._dsp_available_s_since_heartbeat += available_s
        now = time.monotonic()
        if self._dsp_heartbeat_at is None:
            self._dsp_heartbeat_at = now
        elif now - self._dsp_heartbeat_at >= DSP_HEARTBEAT_INTERVAL_S:
            load_factor = (self._dsp_busy_s_since_heartbeat / self._dsp_available_s_since_heartbeat
                            if self._dsp_available_s_since_heartbeat else 0.0)
            logger.info(
                "fl2k (native): DSP load %.1f%% (%.3fs compute time for %.3fs of audio "
                "in the last %.0fs)",
                load_factor * 100.0, self._dsp_busy_s_since_heartbeat,
                self._dsp_available_s_since_heartbeat, now - self._dsp_heartbeat_at)
            self._dsp_busy_s_since_heartbeat = 0.0
            self._dsp_available_s_since_heartbeat = 0.0
            self._dsp_heartbeat_at = now

        return result
