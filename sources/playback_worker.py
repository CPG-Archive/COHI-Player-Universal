####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
import queue
import threading
import time

import numpy as np
from PyQt5.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot

from constants import DATABLOCKSIZE, JUNKSIZE, TIMESCALER
from i18n import tr
from wav_tools import WavHeaderTools

logger = logging.getLogger(__name__)

PROCESS_EVENTS_INTERVAL = 8

PAUSE_HEARTBEAT_INTERVAL_S = 5.0

PAUSE_SILENCE_RATE_MARGIN = 0.9

PAUSE_SILENCE_CHUNK_SIZE = DATABLOCKSIZE // 16

READAHEAD_BLOCKS = 512
READER_QUEUE_TIMEOUT_S = 2.0

BITS_PER_SAMPLE = 16
NORMALIZATION_FACTOR = (2 ** (BITS_PER_SAMPLE - 1)) - 1

INITIAL_GAIN = 0.05

FADE_IN_DURATION_S = 0.05
AGC_REFERENCE_VOLUME = 0.71
AGC_REFERENCE_LEVEL = 8.8874


class _SegmentReader:
    def __init__(self, file_path, start_local_chunk):
        self._queue = queue.Queue(maxsize=READAHEAD_BLOCKS)
        self._stop_event = threading.Event()
        self._finished_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(file_path, start_local_chunk),
            name="segment-reader", daemon=True)
        self._thread.start()

    def _run(self, file_path, local_chunk):
        try:
            with open(file_path, "rb") as f:
                f.seek(216 + local_chunk * JUNKSIZE)
                while not self._stop_event.is_set():
                    block = np.empty(DATABLOCKSIZE, dtype=np.int16)
                    size = f.readinto(block)
                    if size == 0:
                        if self._stop_event.wait(0.1):
                            return
                        size = f.readinto(block)
                        if size == 0:
                            return

                    payload = block[:size // block.itemsize]
                    while not self._stop_event.is_set():
                        try:
                            self._queue.put(payload, timeout=0.2)
                            break
                        except queue.Full:
                            continue
        except OSError as exc:
            logger.error("read-ahead thread aborted (%s): %s", file_path, exc)
        except Exception:
            logger.exception("unexpected error in read-ahead thread (%s)", file_path)
        finally:
            self._finished_event.set()

    def get_next(self, timeout=READER_QUEUE_TIMEOUT_S):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def is_finished(self):
        return self._finished_event.is_set() and self._queue.empty()

    def stop(self):
        self._stop_event.set()


class PlaybackWorker(QObject):
    fileChanged = pyqtSignal(str)
    positionChanged = pyqtSignal(int)
    stateChanged = pyqtSignal(str)
    errorOccurred = pyqtSignal(str)
    trackChanged = pyqtSignal(int)
    deviceConfigNeeded = pyqtSignal(dict)
    shutdownComplete = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.track_index = 0
        self.test_mode = False

        self._backend = None
        self._next_track_index = None
        self._stop_requested = False
        self._send_failed = False
        self._pause_requested = False
        self._wav_muted = False
        self._seek_target = None
        self._gain = INITIAL_GAIN
        self._fade_in_started_at = None
        self._agc_pending = False
        self._agc_diag_next_at = None
        self._shutting_down = False
        self._full_shutdown_requested = False
        self._current_sample_rate = None
        self._current_channels = 1

    @pyqtSlot()
    def start(self):
        if self.config.track_count == 0:
            self.errorOccurred.emit(tr("no WAV files found, playback not possible"))
            return
        self.trackChanged.emit(self.track_index)
        self._request_device_config()

    def _request_device_config(self):
        track = self.config.tracks[self.track_index]
        header = WavHeaderTools().read_header(track[0].file_path)
        if not header:
            self.errorOccurred.emit(tr("invalid header: {file}").format(file=track[0].file_path))
            return

        self._current_sample_rate = int(header["nSamplesPerSec"])
        self._current_channels = max(1, int(header.get("nChannels", 1)))

        self.deviceConfigNeeded.emit({
            "center_freq": int(header["centerfreq"]),
            "sample_rate": self._current_sample_rate,
        })

    @pyqtSlot(object)
    def begin_track(self, backend):
        if self._shutting_down:
            if self._full_shutdown_requested:
                self.shutdownComplete.emit()
            return

        self._backend = backend
        self._stop_requested = False
        self._pause_requested = False
        self._gain = getattr(backend, "default_gain", INITIAL_GAIN)
        self._fade_in_started_at = time.monotonic()
        self._agc_pending = True
        self._agc_diag_next_at = None
        self.stateChanged.emit("playing")

        try:
            self._stream_track(self.config.tracks[self.track_index])
        except Exception as exc:
            logger.exception("unexpected error during playback")
            self.errorOccurred.emit(tr("unexpected error during playback: {error}").format(error=exc))
            self.stateChanged.emit("stopped")
            self._stop_requested = False

        self._backend = None
        if self._shutting_down:
            if self._full_shutdown_requested:
                self.shutdownComplete.emit()
            return
        self._advance_to_next_track()

    def _advance_to_next_track(self):
        if self._send_failed:
            self._send_failed = False
            if self._next_track_index is None:
                self._next_track_index = self.track_index
        if self._next_track_index is not None:
            self.track_index = self._next_track_index
            self._next_track_index = None
        else:
            self.track_index = (self.track_index + 1) % self.config.track_count
        self.trackChanged.emit(self.track_index)
        self._request_device_config()

    def _stream_track(self, segments):
        segment_index = 0
        local_offset_chunks = 0
        global_chunk_count = 0

        while segment_index < len(segments):
            segment = segments[segment_index]
            global_chunk_count = self._stream_segment(segment, local_offset_chunks,
                                                        global_chunk_count)
            if self._stop_requested:
                break

            if self._seek_target is not None:
                segment_index, local_offset_chunks, global_chunk_count = self._seek_target
                self._seek_target = None
            else:
                segment_index += 1
                local_offset_chunks = 0

        self.stateChanged.emit("stopped" if self._stop_requested else "finished")

    def _stream_segment(self, segment, local_offset_chunks, global_chunk_count):
        logger.info("file started: %s", segment.file_path)
        self.fileChanged.emit(segment.file_path)

        silence = np.zeros(DATABLOCKSIZE, dtype=np.int16)
        pause_silence = np.zeros(PAUSE_SILENCE_CHUNK_SIZE, dtype=np.int16)
        junks_per_second = TIMESCALER / JUNKSIZE * 32
        emitted_counter = 0
        events_counter = 0
        local_chunk = local_offset_chunks

        reader = _SegmentReader(segment.file_path, local_chunk)
        reader_started_at = time.monotonic()
        first_block_received = False
        silence_fallback_count = 0
        reported_silence_count = 0
        pause_heartbeat_at = None
        pause_block_count = 0
        pause_started_at = None
        pause_chunks_sent = 0
        try:
            self.positionChanged.emit(global_chunk_count)

            while not self._stop_requested and self._seek_target is None:
                events_counter += 1
                if self._pause_requested or events_counter >= PROCESS_EVENTS_INTERVAL:
                    QCoreApplication.processEvents()
                    events_counter = 0

                if self._stop_requested or self._seek_target is not None:
                    break

                if self._pause_requested or self._wav_muted:
                    if not self._backend.silence_required_during_mute:
                        pause_started_at = None
                        time.sleep(0.05)
                        continue

                    if pause_started_at is None:
                        pause_started_at = time.monotonic()
                        pause_chunks_sent = 0

                    if not self._send_block(pause_silence, PAUSE_SILENCE_CHUNK_SIZE):
                        self._stop_requested = True
                        break
                    pause_chunks_sent += 1

                    if self._current_sample_rate:
                        chunk_duration_s = (PAUSE_SILENCE_CHUNK_SIZE / self._current_channels
                                             / self._current_sample_rate)
                        target_elapsed_s = (pause_chunks_sent * chunk_duration_s
                                             * PAUSE_SILENCE_RATE_MARGIN)
                        actual_elapsed_s = time.monotonic() - pause_started_at
                        sleep_s = target_elapsed_s - actual_elapsed_s
                        if sleep_s > 0:
                            time.sleep(sleep_s)

                    pause_block_count += 1
                    now = time.monotonic()
                    if pause_heartbeat_at is None:
                        pause_heartbeat_at = now
                    elif now - pause_heartbeat_at >= PAUSE_HEARTBEAT_INTERVAL_S:
                        logger.info("pause active: %d silence chunk(s) sent in the last %.0fs",
                                     pause_block_count, now - pause_heartbeat_at)
                        pause_heartbeat_at = now
                        pause_block_count = 0
                    continue

                pause_started_at = None

                block = reader.get_next(timeout=0)
                if block is None:
                    if reader.is_finished():
                        break
                    silence_fallback_count += 1
                    if not self._send_block(silence, DATABLOCKSIZE):
                        self._stop_requested = True
                        break
                    continue

                if not first_block_received:
                    first_block_received = True
                    startup_latency = time.monotonic() - reader_started_at
                    if startup_latency > 0.05:
                        logger.info(
                            "read-ahead thread for %s needed %.3fs for the first block "
                            "(%d silence block(s) sent in the meantime)",
                            os.path.basename(segment.file_path), startup_latency,
                            silence_fallback_count)

                if not self._send_block(block, len(block)):
                    self._stop_requested = True
                    break

                local_chunk += 1
                global_chunk_count += 1
                emitted_counter += 1
                if emitted_counter > junks_per_second:
                    self.positionChanged.emit(global_chunk_count)
                    emitted_counter = 0

                    if silence_fallback_count > reported_silence_count:
                        logger.info(
                            "%s: %d silence block(s) sent since the last report because of "
                            "read stalls (total since segment start: %d)",
                            os.path.basename(segment.file_path),
                            silence_fallback_count - reported_silence_count,
                            silence_fallback_count)
                        reported_silence_count = silence_fallback_count

                    if self._agc_pending:
                        new_gain = self._calculate_autogain(block)
                        if new_gain is not None:
                            self._gain = new_gain
                        self._agc_pending = False
                        if self.config.agc_interval > 0:
                            self._agc_diag_next_at = time.monotonic() + self.config.agc_interval
                    elif self.config.agc_interval > 0 and self._agc_diag_next_at is not None:
                        now = time.monotonic()
                        if now >= self._agc_diag_next_at:
                            self._calculate_autogain(block, diagnostic_only=True)
                            self._agc_diag_next_at = now + self.config.agc_interval
        finally:
            reader.stop()

        return global_chunk_count

    def _calculate_autogain(self, raw_samples, diagnostic_only=False):
        if len(raw_samples) < 256:
            return None

        cv = (raw_samples[0:-1:2].astype(np.float32)
              + 1j * raw_samples[1::2].astype(np.float32)) * 32

        level = 1.5 * np.std(np.abs(cv)) / NORMALIZATION_FACTOR / AGC_REFERENCE_VOLUME
        if not np.isfinite(level) or level <= 0:
            logger.warning("AGC%s: no valid level measurable, keeping the previous gain",
                            " diagnostic" if diagnostic_only else "")
            return None

        target = self.config.autovol
        nominal_gain = target * self._backend.default_gain
        new_gain = nominal_gain * (AGC_REFERENCE_LEVEL / level)
        if diagnostic_only:
            logger.info(
                "AGC diagnostic (not applied): measured level=%.4f, autovol=%.4f, "
                "would-be gain=%.4f, gain still in effect=%.4f",
                level, target, new_gain, self._gain)
        else:
            logger.info("AGC: measured level=%.4f, autovol=%.4f, new gain=%.4f",
                         level, target, new_gain)
        return new_gain

    def _send_block(self, data, size):
        if self.test_mode:
            return True

        try:
            payload = self._backend.encode_samples(data[:size], self._effective_gain())
            self._backend.data_sock.send(payload)
        except Exception as exc:
            logger.error("error while sending streaming data: %s", exc)
            self.errorOccurred.emit(tr("streaming error: {error}").format(error=exc))
            self._send_failed = True
            return False
        return True

    def _effective_gain(self):
        if self._fade_in_started_at is None:
            return self._gain
        elapsed = time.monotonic() - self._fade_in_started_at
        if elapsed >= FADE_IN_DURATION_S:
            self._fade_in_started_at = None
            return self._gain
        return self._gain * (elapsed / FADE_IN_DURATION_S)

    @pyqtSlot()
    def next_track(self):
        self._request_track_switch((self.track_index + 1) % self.config.track_count)

    @pyqtSlot()
    def previous_track(self):
        self._request_track_switch((self.track_index - 1) % self.config.track_count)

    @pyqtSlot()
    def restart_track(self):
        self._request_track_switch(self.track_index)

    @pyqtSlot(int)
    def select_track(self, index):
        if 0 <= index < self.config.track_count:
            self._request_track_switch(index)
        else:
            logger.warning("select_track: index %d out of valid range (0..%d)",
                            index, self.config.track_count - 1)

    def _request_track_switch(self, index):
        self._next_track_index = index
        self._stop_requested = True

    @pyqtSlot()
    def pause(self):
        logger.info("pause requested")
        self._pause_requested = True
        self.stateChanged.emit("paused")

    @pyqtSlot()
    def resume(self):
        logger.info("continue requested")
        self._pause_requested = False
        self.stateChanged.emit("playing")

    @pyqtSlot(bool)
    def set_wav_muted(self, muted):
        logger.info("COHI-Player (WAV) %s", "muted" if muted else "active again")
        self._wav_muted = muted

    @pyqtSlot(str, int, int)
    def seek(self, file_path, local_offset_chunks, segment_start_chunk):
        segments = self.config.tracks[self.track_index]
        for index, segment in enumerate(segments):
            if segment.file_path == file_path:
                global_chunk_count = segment_start_chunk + local_offset_chunks
                logger.info("seek requested: %s, position %d", os.path.basename(file_path),
                            global_chunk_count)
                self._seek_target = (index, local_offset_chunks, global_chunk_count)
                return
        logger.warning("seek target not found in the current track: %s", file_path)

    def request_stop(self, full_shutdown=False):
        self._shutting_down = True
        self._full_shutdown_requested = full_shutdown
        self._stop_requested = True
        self._pause_requested = False
