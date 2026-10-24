####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import struct

import numpy as np

UDP_MAGIC = b"CDBG"
UDP_HEADER_SIZE = 8


def decode_udp_debug_packet(data):
    if len(data) < UDP_HEADER_SIZE:
        return None
    if data[0:4] != UDP_MAGIC:
        return None
    (n_samples,) = struct.unpack_from("<I", data, 4)
    payload = data[UDP_HEADER_SIZE:UDP_HEADER_SIZE + n_samples]
    if len(payload) != n_samples:
        return None
    raw = np.frombuffer(payload, dtype=np.int8)
    return raw.astype(np.float32) / 128.0


def compute_spectrum(samples, fft_size=4096, window="hann"):
    samples = np.asarray(samples)
    is_complex = np.iscomplexobj(samples)

    if len(samples) >= fft_size:
        block = samples[-fft_size:]
    else:
        block = np.pad(samples, (0, fft_size - len(samples)))

    if window == "hann":
        win = np.hanning(fft_size)
        win = win / win.mean()
        block = block * win

    if is_complex:
        spectrum = np.fft.fftshift(np.fft.fft(block))
    else:
        spectrum = np.fft.rfft(block)

    magnitude = np.abs(spectrum) / fft_size
    magnitude_db = 20.0 * np.log10(np.maximum(magnitude, 1e-6))
    return magnitude_db.astype(np.float32)


def spectrum_freq_axis(fft_size, sample_rate, is_complex):
    if is_complex:
        freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate))
    else:
        freqs = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    return freqs.astype(np.float32)


class WaterfallBuffer:
    def __init__(self, n_bins, n_rows=200, fill_value=-120.0):
        self.n_bins = n_bins
        self.n_rows = n_rows
        self._data = np.full((n_rows, n_bins), fill_value, dtype=np.float32)

    def push(self, spectrum_db):
        if len(spectrum_db) != self.n_bins:
            raise ValueError(
                f"spectrum length {len(spectrum_db)} does not match n_bins={self.n_bins}")
        self._data = np.roll(self._data, -1, axis=0)
        self._data[-1, :] = spectrum_db

    def get_image(self):
        return self._data.copy()


class SampleAccumulator:
    def __init__(self, max_size):
        self.max_size = max_size
        self._buffer = None

    def push(self, samples):
        if self._buffer is None:
            self._buffer = np.asarray(samples)
        else:
            self._buffer = np.concatenate([self._buffer, samples])
        if len(self._buffer) > self.max_size:
            self._buffer = self._buffer[-self.max_size:]

    def get_buffer(self):
        if self._buffer is None:
            return np.array([], dtype=np.float32)
        return self._buffer

    def reset(self):
        self._buffer = None


class SpectrumAverager:
    def __init__(self, count=1):
        self._avg = None
        self.set_count(count)

    def set_count(self, count):
        count = max(1, int(count))
        self.count = count
        self._alpha = 2.0 / (count + 1)

    def push(self, spectrum_db):
        spectrum_db = np.asarray(spectrum_db, dtype=np.float64)
        if self._avg is None or len(self._avg) != len(spectrum_db):
            self._avg = spectrum_db.copy()
        else:
            self._avg = self._alpha * spectrum_db + (1.0 - self._alpha) * self._avg
        return self._avg.astype(np.float32)

    def reset(self):
        self._avg = None
