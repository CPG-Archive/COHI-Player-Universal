####################################################
# COHI-Player-Universal
# Output backend smiSDR / parlioSDR with FPGA DUC (hwconfig: smisdr_iq)
####################################################

# Baseband I/Q output with in-band signalling for the DUC gateware of
# smiSDR / parlioSDR (https://github.com/radiolab81/smisdr, directory gateware).
# The protocol follows DspWorker::run_IQ_engine of COHIRADIAStreamer.
#
# Contrary to hwconfig: smisdr (finished RF samples, upconversion in
# cohi_native_upconverter on the host) only the I/Q baseband of the WAV
# recording is sent here, at its native sample rate. NCO, rate conversion and
# mixing to the RF carrier are done by the FPGA.
#
#   control port (default 5000, TCP, OPTIONAL, one short connection per command)
#       "width 16\n"            DAC bus is always 16 bit wide
#       "rate <MSps>\n"         BUS rate = 2 x I/Q sample rate, because I and Q
#                               are transferred one after the other as 16 bit words
#   data port (default 1234, TCP): one long connection, 16 bit little endian words
#       bit 15:14 = 00   I sample, 14 bit signed in bit 13:0
#       bit 15:14 = 01   Q sample, 14 bit signed in bit 13:0
#       bit 15:14 = 10   command word: bit 13:8 = index, bit 7:0 = payload
#       bit 15:14 = 11   execute: payload 'E'
#
#   A command is: (10, 63, opcode), (10, 0..3, FTW byte 0..3 LSB first), (11, 0, 'E')
#       'S'  NCO frequency shift (centre frequency of the recording), FTW = f / ref_clk * 2^32
#       'R'  sample rate of the incoming I/Q data,                  FTW = fs / ref_clk * 2^32
#
# Both commands are sent at the start of every track whose centre frequency or
# sample rate differs from the previous one, in-band at the start of the data
# stream, before the first I/Q sample.
#
# All three program variants use this output:
#   COHI-Player         WAV baseband -> 14 bit words (encode_samples)
#   COHI-Stream         internet stations -> cohi_stemlab_live_upconverter (complex
#                       baseband float32 around the LO) -> 14 bit words
#   COHI-Player+Stream  both added in the baseband, then 14 bit words
# For the live variants the LO frequency / sample rate (stemlab_live_lo_freq,
# stemlab_live_sample_rate or LOFREQ/SAMPLERATE of the station list) become the NCO
# and input rate of the DUC; in COHI-Player+Stream they come from the WAV file.

import logging
import socket
import threading
import time

import numpy as np

from device_backend import DeviceBackend
from i18n import tr
from iq_live_mixin import IqLiveMixin

logger = logging.getLogger(__name__)

DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_DATA_PORT = 1234
DEFAULT_CONTROL_PORT = 5000

# reference clock of the DUC gateware (NCO / rate word are fractions of it);
# the output sample rate of the DUC equals this clock (50 MHz -> up to 25 MHz RF)
DEFAULT_REF_CLOCK_HZ = 50_000_000.0

# highest signal edge relative to the Nyquist frequency (ref_clk / 2)
MAX_EDGE_OF_NYQUIST = 0.95

# protocol: tags in bit 15:14 of every 16 bit word
TAG_I = 0
TAG_Q = 1
TAG_CMD = 2
TAG_EXEC = 3
CMD_INIT_INDEX = 63
CMD_SHIFT = ord("S")
CMD_RATE = ord("R")
CMD_EXECUTE = ord("E")

IQ_BITS = 14
IQ_MIN = -(1 << (IQ_BITS - 1))          # -8192
IQ_MAX = (1 << (IQ_BITS - 1)) - 1       # +8191
IQ_MASK = (1 << IQ_BITS) - 1            # 0x3FFF
INPUT_TO_IQ_SHIFT = 16 - IQ_BITS        # 16 bit WAV -> 14 bit protocol: /4

# The gateware (multirate_upsampler.v) knows exactly three input rates and picks
# one by thresholds on the rate word: any other rate would be treated as the
# nearest class and play back at the wrong speed / frequency.
SUPPORTED_RATES_HZ = (250_000, 500_000, 1_250_000)
RATE_TOLERANCE = 0.001

CONTROL_TIMEOUT_S = 0.5
DATA_CONNECT_TIMEOUT_S = 3.0
SETTLE_AFTER_CONTROL_S = 0.05
SEND_BUFFER_BYTES = 512 * 1024

# Real-time pacing as in cohi_native_upconverter (hwconfig: smisdr): the deadline moves on
# by the duration of every block; the sender may run PACING_LEAD_S ahead of real time, and
# if it falls more than PACING_MAX_BEHIND_S behind (track change, pause) the time base is
# set anew instead of catching up with a burst.
# Unit of time: one I/Q pair = 2 bus words (I, Q) = 1 / sample_rate seconds, i.e. the bus
# runs at 2 x sample_rate words/s (the "rate" command sets exactly that).
PACING_LEAD_S = 0.100
PACING_MAX_BEHIND_S = 0.250

DEBUG_TAP_MIN_INTERVAL_S = 0.1
CLOSE_SILENCE_PAIRS = 256
# Float domain of the live paths: the live upconverter delivers modulated carriers with
# peaks of up to ~1.8 (carrier 1.0 + modulation, measured 1.65 ... 1.76 for 1 ... 17 stations),
# so +-2.0 is full scale of the 14 bit interface. The WAV baseband is scaled in this domain
# such that it has the same level as in COHI-Player (16 bit / 4 = 14 bit).
FLOAT_FULL_SCALE = 2.0
FLOAT_TO_IQ_SCALE = IQ_MAX / FLOAT_FULL_SCALE
NORMALIZATION_FACTOR = 32767.0          # 16 bit WAV samples -> float +-1.0
LIMITER_RELEASE_PER_S = 1.0             # gain recovery of the overload limiter per second
LIMITER_RAMP_SAMPLES = 128              # I/Q pairs over which a gain change is faded


def make_cmd_word(ctrl, index, payload):
    """16 bit word: bit 15:14 = ctrl, bit 13:8 = index, bit 7:0 = payload."""
    return ((ctrl & 0x03) << 14) | ((index & 0x3F) << 8) | (payload & 0xFF)


def calculate_ftw(frequency_hz, ref_clk_hz=DEFAULT_REF_CLOCK_HZ):
    """Phase tuning word (32 bit): frequency / ref_clk * 2^32."""
    ftw = int(round(float(frequency_hz) / float(ref_clk_hz) * 4294967296.0))
    if not 0 <= ftw <= 0xFFFFFFFF:
        raise ValueError("frequency {} Hz outside the range of the tuning word "
                         "(reference clock {} Hz)".format(frequency_hz, ref_clk_hz))
    return ftw


def build_command(opcode, ftw):
    """In-band command with a 32 bit value as little endian 16 bit words."""
    return [
        make_cmd_word(TAG_CMD, CMD_INIT_INDEX, opcode),
        make_cmd_word(TAG_CMD, 0, ftw & 0xFF),
        make_cmd_word(TAG_CMD, 1, (ftw >> 8) & 0xFF),
        make_cmd_word(TAG_CMD, 2, (ftw >> 16) & 0xFF),
        make_cmd_word(TAG_CMD, 3, (ftw >> 24) & 0xFF),
        make_cmd_word(TAG_EXEC, 0, CMD_EXECUTE),
    ]


def build_init_sequence(center_freq_hz, sample_rate_hz, ref_clk_hz=DEFAULT_REF_CLOCK_HZ):
    """Bytes for the start of a track: 'S' (NCO) followed by 'R' (input rate)."""
    words = (build_command(CMD_SHIFT, calculate_ftw(center_freq_hz, ref_clk_hz))
             + build_command(CMD_RATE, calculate_ftw(sample_rate_hz, ref_clk_hz)))
    return np.asarray(words, dtype="<u2").tobytes()


def encode_iq_words(samples, gain):
    """Interleaved int16 I/Q (as in the WAV file) -> tagged 16 bit words.

    gain 1.0 corresponds to the plain 16 -> 14 bit reduction of run_IQ_engine
    (arithmetic shift by 2); values are limited to the 14 bit range."""
    count = (len(samples) // 2) * 2
    scaled = np.rint(samples[:count].astype(np.float32) * np.float32(gain / (1 << INPUT_TO_IQ_SHIFT)))
    return _tag_values(np.clip(scaled, IQ_MIN, IQ_MAX))


def _tag_values(values):
    """14 bit signed values (I, Q, I, Q ...) -> tagged 16 bit words."""
    masked = values.astype(np.int16).astype(np.uint16) & IQ_MASK
    words = np.empty(len(masked), dtype="<u2")
    words[0::2] = masked[0::2] | (TAG_I << 14)
    words[1::2] = masked[1::2] | (TAG_Q << 14)
    return words.tobytes()


def encode_float_iq_words(payload, gains=None):
    """Interleaved float32 I/Q (+-FLOAT_FULL_SCALE = full scale) -> tagged words, limited to
    14 bit. gains: optional per-sample gain vector (overload limiter)."""
    arr = np.frombuffer(payload, dtype=np.float32)
    count = (len(arr) // 2) * 2
    scaled = np.nan_to_num(arr[:count]) * np.float32(FLOAT_TO_IQ_SCALE)
    if gains is not None:
        scaled = scaled * gains[:count]
    return _tag_values(np.clip(np.rint(scaled), IQ_MIN, IQ_MAX))


def decode_iq_words(payload):
    """Tagged words -> complex baseband in the range +-1 (for the debug displays)."""
    words = np.frombuffer(payload, dtype="<u2")
    count = (len(words) // 2) * 2
    raw = (words[:count] & IQ_MASK).astype(np.int32)
    raw = np.where(raw > IQ_MAX, raw - (1 << IQ_BITS), raw).astype(np.float32) / float(1 << (IQ_BITS - 1))
    return raw[0::2] + 1j * raw[1::2]


def supported_rate(sample_rate_hz):
    """Nominal DUC input rate for a recording rate, or None if not supported."""
    for rate in SUPPORTED_RATES_HZ:
        if abs(sample_rate_hz - rate) <= rate * RATE_TOLERANCE:
            return rate
    return None


def format_bus_rate_command(bus_rate_hz):
    """'rate <MSps>' as in run_IQ_engine (250 kSps -> 0.5, 500 kSps -> 1.0, 1.25 MSps -> 2.5)."""
    return "rate {:.1f}\n".format(bus_rate_hz / 1_000_000.0)


class _PacedIqSocket:
    """Socket-like object for PlaybackWorker: paces the output to the I/Q sample
    rate (like the pacing in run_IQ_engine), feeds the debug tap and remembers
    transmission errors."""

    def __init__(self, sock, sample_rate, backend):
        self._sock = sock
        self._sample_rate = float(sample_rate)
        self._backend = backend
        self._deadline = None
        self.failed = False
        self._send_lock = threading.Lock()

    def send(self, payload):
        payload = bytes(payload)
        try:
            with self._send_lock:
                self._sock.sendall(payload)
        except OSError:
            self.failed = True
            raise
        self._backend._maybe_call_debug_tap(payload)
        self._pace(len(payload) // 4)
        return len(payload)

    def sendall(self, payload):
        self.send(payload)

    def _pace(self, iq_pairs):
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now
        self._deadline += iq_pairs / self._sample_rate      # = bus words / (2 * sample_rate)
        if now - self._deadline > PACING_MAX_BEHIND_S:
            self._deadline = now                            # do not catch up with a burst
        elif self._deadline - PACING_LEAD_S > now:
            time.sleep(self._deadline - PACING_LEAD_S - now)

    def send_unpaced(self, payload):
        with self._send_lock:
            self._sock.sendall(payload)

    def settimeout(self, *args, **kwargs):
        return self._sock.settimeout(*args, **kwargs)

    def close(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class _FloatIqSink:
    """For the live paths: takes float32 I/Q (as the live upconverter delivers it, in
    COHI-Player+Stream already added to the WAV baseband).

    Clipping at the 14 bit limit would produce intermodulation products (audible as
    heterodyne whistles), so a slow peak limiter reduces the gain when a block would
    overload; normally (single source) it does not intervene."""

    def __init__(self, paced_socket, iq_sample_rate):
        self._paced = paced_socket
        self._rate = float(iq_sample_rate)
        self._gain = 1.0
        self._last_log = 0.0

    def sendall(self, payload):
        arr = np.frombuffer(payload, dtype=np.float32)
        count = (len(arr) // 2) * 2
        if count == 0:
            return
        peak = float(np.max(np.abs(np.nan_to_num(arr[:count])))) * FLOAT_TO_IQ_SCALE
        new_gain = min(1.0, self._gain + (count / 2) / self._rate * LIMITER_RELEASE_PER_S)
        if peak * new_gain > IQ_MAX:
            new_gain = IQ_MAX / peak
            now = time.monotonic()
            if now - self._last_log > 10.0:
                self._last_log = now
                logger.info("smisdr_iq: level limiter active (block peak %.0f%% of full scale, gain %.2f)",
                            peak / IQ_MAX * 100.0, new_gain)
        gains = None
        if new_gain != 1.0 or self._gain != 1.0:
            gains = np.full(count, new_gain, dtype=np.float32)
            if new_gain != self._gain:
                ramp = min(LIMITER_RAMP_SAMPLES, count // 2)
                gains[:2 * ramp] = np.repeat(
                    np.linspace(self._gain, new_gain, ramp, dtype=np.float32), 2)
        self._gain = new_gain
        self._paced.send(encode_float_iq_words(payload, gains))


class SmisdrIqBackend(IqLiveMixin, DeviceBackend):
    """smiSDR / parlioSDR with DUC gateware: baseband I/Q with in-band signalling."""

    default_gain = 1.0

    def __init__(self, address=DEFAULT_ADDRESS, data_port=DEFAULT_DATA_PORT,
                 control_port=DEFAULT_CONTROL_PORT, ref_clock_hz=DEFAULT_REF_CLOCK_HZ,
                 live_enabled=False, program_variant="COHI-Player", live_stations_file=None,
                 live_upconverter_tool_path=None, live_control_port=0, live_sferics_port=0,
                 parent=None):
        super().__init__(parent)
        self._init_live(live_enabled, program_variant, live_stations_file,
                        live_upconverter_tool_path, live_control_port, live_sferics_port)
        self._paced = None
        self._float_mode = False
        self.address = address or DEFAULT_ADDRESS
        self.data_port = int(data_port)
        self.control_port = control_port        # None = no control port (fixed hardware)
        self.ref_clock_hz = float(ref_clock_hz)
        self.data_sock = None
        self._current_params = None
        self._debug_tap_callback = None
        self._debug_tap_last_sent = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def identify():
        return {
            "rates": {"values": list(SUPPORTED_RATES_HZ)},
            "rate_type": "discrete",
            "RX": False,
            "TX": True,
            "device_name": "smisdr_iq",
            "connection_type": "TCP/IP (smiSDR / parlioSDR, baseband I/Q with in-band "
                               "signalling, DUC in the FPGA)",
        }

    # ------------------------------------------------------------------
    # debug displays (same interface as the STEMlab backend: complex I/Q + LO)
    # ------------------------------------------------------------------
    def set_debug_tap(self, callback):
        self._debug_tap_callback = callback

    def _maybe_call_debug_tap(self, payload):
        if self._debug_tap_callback is None:
            return
        now = time.monotonic()
        if now - self._debug_tap_last_sent < DEBUG_TAP_MIN_INTERVAL_S:
            return
        self._debug_tap_last_sent = now
        try:
            lo_freq_hz = float(self._current_params[1]) if self._current_params else 0.0
            self._debug_tap_callback(decode_iq_words(payload), lo_freq_hz)
        except Exception:
            logger.exception("error in debug tap callback (smisdr_iq) - ignored")

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect_device(self, configured_address):
        # No probe connection here: a connect + close would use up the single accept of
        # servers that take only one connection per port (e.g. the TCP Source blocks of
        # smisdr_sim.grc). Reachability is reported by configure_track(), which follows
        # immediately and has to connect anyway.
        logger.info("smisdr_iq: connect_device (%s, control port %s, data port %d)",
                    self.address, self.control_port or "off", self.data_port)
        if self.control_port is None:
            logger.info("smisdr_iq: no control port configured - width/rate are not sent, the "
                        "hardware must already be set to 16 bit and 2 x the sample rate of the baseband")
        return "{}:{}".format(self.address, self.data_port)

    def _send_control_command(self, command):
        try:
            with socket.create_connection((self.address, self.control_port),
                                          timeout=CONTROL_TIMEOUT_S) as ctrl:
                ctrl.settimeout(CONTROL_TIMEOUT_S)
                ctrl.sendall(command.encode("ascii"))
        except OSError as exc:
            logger.error("smisdr_iq: control command %r failed: %s", command.strip(), exc)
            return False
        logger.info("smisdr_iq: control command sent: %s", command.strip())
        return True

    # ------------------------------------------------------------------
    # track configuration
    # ------------------------------------------------------------------
    def _pipeline_alive(self):
        return self._paced is not None and not self._paced.failed

    def configure_track(self, sample_rate, center_freq):
        nominal_rate = supported_rate(sample_rate)
        if nominal_rate is not None:
            sample_rate = nominal_rate
        if self._pipeline_alive() and self._current_params == (sample_rate, center_freq):
            logger.info("smisdr_iq: same frequency/sample rate as the active connection - "
                        "no reconfiguration needed")
            self.infoMessage.emit(tr("output pipeline kept unchanged"))
            return True

        logger.info("smisdr_iq: configure_track started (sample_rate=%s, center_freq=%s)",
                    sample_rate, center_freq)
        self._close_data_connection()

        if nominal_rate is None:
            logger.error("smisdr_iq: sample rate %s Hz is not supported by the DUC gateware", sample_rate)
            self.errorOccurred.emit(tr(
                "sample rate {rate} Hz of the recording is not supported by the DUC "
                "gateware (only 250000, 500000 and 1250000 Hz)"
            ).format(rate=int(sample_rate)))
            return False

        upper_edge = center_freq + sample_rate / 2.0
        if upper_edge > MAX_EDGE_OF_NYQUIST * self.ref_clock_hz / 2.0:
            logger.error("smisdr_iq: signal up to %.0f Hz does not fit below Nyquist (%.0f Hz) "
                         "of the DUC clock %.0f Hz", upper_edge, self.ref_clock_hz / 2.0,
                         self.ref_clock_hz)
            self.errorOccurred.emit(tr(
                "recording (up to {edge} Hz) is above the usable range of the DUC "
                "({limit} Hz) - check smisdr_iq_ref_clock"
            ).format(edge=int(upper_edge), limit=int(MAX_EDGE_OF_NYQUIST * self.ref_clock_hz / 2.0)))
            return False
        try:
            init_bytes = build_init_sequence(center_freq, sample_rate, self.ref_clock_hz)
        except ValueError as exc:
            self.errorOccurred.emit(str(exc))
            return False

        if self.control_port is not None:
            bus_rate_command = format_bus_rate_command(2.0 * sample_rate)
            if not (self._send_control_command("width 16\n")
                    and self._send_control_command(bus_rate_command)):
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
            sock.sendall(init_bytes)
        except OSError as exc:
            logger.error("smisdr_iq: data port %s:%d not reachable: %s",
                         self.address, self.data_port, exc)
            self.errorOccurred.emit(tr(
                "smiSDR/parlioSDR data port {address}:{port} not reachable ({error})"
            ).format(address=self.address, port=self.data_port, error=exc))
            return False

        with self._lock:
            self._paced = _PacedIqSocket(sock, sample_rate, self)
            self.data_sock = self._paced
            self._current_params = (sample_rate, center_freq)
            self._float_mode = False
        logger.info("smisdr_iq: in-band init sent: NCO %s Hz (FTW 0x%08X), input rate %s Hz "
                    "(FTW 0x%08X)", center_freq, calculate_ftw(center_freq, self.ref_clock_hz),
                    sample_rate, calculate_ftw(sample_rate, self.ref_clock_hz))
        self.infoMessage.emit(tr(
            "smisdr_iq output started (NCO {freq} Hz, I/Q {rate} kSps, DUC in the FPGA)"
        ).format(freq=int(center_freq), rate="{:g}".format(sample_rate / 1000.0)))

        if self.live_enabled:
            wav_buffer = self._begin_live(center_freq, sample_rate, _FloatIqSink(self._paced, sample_rate))
            if wav_buffer is not None:
                self.data_sock = wav_buffer
                self._float_mode = True
        return True

    # ------------------------------------------------------------------
    # samples
    # ------------------------------------------------------------------
    def encode_samples(self, samples, gain):
        if self._float_mode:
            # COHI-Player+Stream: float32 baseband, added to the live baseband and then
            # converted by _FloatIqSink (same scaling as the STEMlab backend)
            return gain * samples.astype(np.float32) * np.float32(FLOAT_FULL_SCALE / NORMALIZATION_FACTOR)
        return encode_iq_words(samples, gain)

    # ------------------------------------------------------------------
    # end
    # ------------------------------------------------------------------
    def _close_data_connection(self, send_silence=False):
        if self.live_enabled:
            self._end_live()
        with self._lock:
            sock = self._paced
            wav_buffer = self.data_sock if self.data_sock is not sock else None
            self._paced = None
            self.data_sock = None
            self._current_params = None
            self._float_mode = False
        if wav_buffer is not None:
            wav_buffer.close()
        if sock is None:
            return
        if send_silence and not sock.failed:
            # leave the DAC at zero instead of the last sample value
            try:
                sock.send_unpaced(encode_iq_words(np.zeros(2 * CLOSE_SILENCE_PAIRS, dtype=np.int16), 1.0))
            except OSError:
                pass
        sock.close()

    def shutdown(self):
        self.close()

    def close(self):
        self.infoMessage.emit(tr("stopping smisdr_iq output ..."))
        self._close_data_connection(send_silence=True)
        self.infoMessage.emit(tr("smisdr_iq output stopped"))
