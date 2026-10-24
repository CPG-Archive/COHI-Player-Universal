####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import socket
import threading

import numpy as np
from i18n import tr
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

try:
    import pyqtgraph as pg
    _PYQTGRAPH_AVAILABLE = True
except ImportError:
    _PYQTGRAPH_AVAILABLE = False

from debug_signal_utils import (
    SampleAccumulator, SpectrumAverager, WaterfallBuffer, compute_spectrum,
    decode_udp_debug_packet, spectrum_freq_axis,
)

logger = logging.getLogger(__name__)

FFT_SIZE = 32768
WATERFALL_ROWS = 200
TIME_DOMAIN_DISPLAY_SAMPLES = 2048


class DebugDataBridge(QObject):
    dataReady = pyqtSignal(object, float, float)


def fl2k_udp_listener_loop(port, bridge, sample_rate, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    sock.settimeout(0.5)
    try:
        while not stop_event.is_set():
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            samples = decode_udp_debug_packet(data)
            if samples is None:
                continue
            bridge.dataReady.emit(samples, sample_rate, 0.0)
    finally:
        sock.close()


def _make_debug_udp_socket_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if _PYQTGRAPH_AVAILABLE:
    pg.setConfigOptions(antialias=False, background="k", foreground="w")

    class SpectrumWaterfallWindow(QWidget):
        def __init__(self, bridge, is_complex, parent=None):
            super().__init__(parent)
            self.is_complex = is_complex
            self.setWindowTitle(tr("COHI-Player Debug: Spectrum / Waterfall"))
            self.resize(800, 700)

            self._accumulator = SampleAccumulator(max_size=FFT_SIZE)
            self._averager = SpectrumAverager(count=4)
            self._waterfall_buffer = None
            self._sample_rate = None
            self._freqs_full = None

            layout = QVBoxLayout(self)

            control_row = QHBoxLayout()
            control_row.addWidget(QLabel(tr("Frequency range up to:")))
            self.max_freq_spinbox = QDoubleSpinBox()
            self.max_freq_spinbox.setRange(0.01, 50.0)
            self.max_freq_spinbox.setValue(2.0)
            self.max_freq_spinbox.setSingleStep(0.5)
            self.max_freq_spinbox.setSuffix(" MHz")
            control_row.addWidget(self.max_freq_spinbox)

            control_row.addWidget(QLabel(tr("Smoothing:")))
            self.averaging_spinbox = QSpinBox()
            self.averaging_spinbox.setRange(1, 50)
            self.averaging_spinbox.setValue(4)
            self.averaging_spinbox.valueChanged.connect(self._averager.set_count)
            control_row.addWidget(self.averaging_spinbox)

            control_row.addStretch(1)
            layout.addLayout(control_row)

            self.spectrum_plot = pg.PlotWidget(title=tr("Spectrum"))
            self.spectrum_plot.setLabel("bottom", tr("Frequency"), units="Hz")
            self.spectrum_plot.setLabel("left", tr("Level"), units="dB")
            self.spectrum_plot.setYRange(-120, 0)
            self.spectrum_curve = self.spectrum_plot.plot([], [], pen="y")
            layout.addWidget(self.spectrum_plot, stretch=1)

            self.waterfall_plot = pg.PlotWidget(title="Waterfall")
            self.waterfall_plot.setLabel("bottom", tr("Frequency"), units="Hz")
            self.waterfall_image = pg.ImageItem()
            self.waterfall_plot.addItem(self.waterfall_image)
            self.waterfall_image.setLookupTable(pg.colormap.get("viridis").getLookupTable())
            self.waterfall_image.setLevels((-120, 0))
            layout.addWidget(self.waterfall_plot, stretch=1)

            self.waterfall_plot.setXLink(self.spectrum_plot)

            bridge.dataReady.connect(self._on_data, type=Qt.QueuedConnection)

        def _crop_indices(self):
            max_freq_hz = self.max_freq_spinbox.value() * 1e6
            if self.is_complex:
                mask = (self._freqs_full >= -max_freq_hz) & (self._freqs_full <= max_freq_hz)
            else:
                mask = self._freqs_full <= max_freq_hz
            idx = np.flatnonzero(mask)
            if len(idx) == 0:
                idx = np.array([0])
            return int(idx[0]), int(idx[-1]) + 1

        def _on_data(self, samples, sample_rate, lo_freq_hz=0.0):
            if bool(np.iscomplexobj(samples)) != self.is_complex:
                return

            if self._sample_rate != sample_rate:
                self._sample_rate = sample_rate
                self._accumulator.reset()
                self._averager.reset()
                self._freqs_full = spectrum_freq_axis(FFT_SIZE, sample_rate, self.is_complex)
                self._waterfall_buffer = None

            self._accumulator.push(samples)
            spectrum_db_full_raw = compute_spectrum(self._accumulator.get_buffer(), fft_size=FFT_SIZE)
            spectrum_db_full = self._averager.push(spectrum_db_full_raw)

            start, stop = self._crop_indices()
            freqs = self._freqs_full[start:stop] + lo_freq_hz
            spectrum_db = spectrum_db_full[start:stop]
            self.spectrum_curve.setData(freqs, spectrum_db)

            if self._waterfall_buffer is None or self._waterfall_buffer.n_bins != len(spectrum_db):
                self._waterfall_buffer = WaterfallBuffer(n_bins=len(spectrum_db), n_rows=WATERFALL_ROWS)
            self._waterfall_buffer.push(spectrum_db)
            image = self._waterfall_buffer.get_image()
            self.waterfall_image.setImage(image.T[:, ::-1], autoLevels=False)
            self.waterfall_image.setRect(freqs[0], 0, freqs[-1] - freqs[0], WATERFALL_ROWS)

    class TimeDomainWindow(QWidget):
        def __init__(self, bridge, is_complex, parent=None):
            super().__init__(parent)
            self.is_complex = is_complex
            self._paused = False
            self._min_value = None
            self._max_value = None
            self.setWindowTitle(tr("COHI-Player Debug: Waveform"))
            self.resize(800, 440)

            layout = QVBoxLayout(self)

            button_row = QHBoxLayout()
            self.stop_button = QPushButton(tr("Stop"))
            self.stop_button.setCheckable(True)
            self.stop_button.clicked.connect(self._on_stop_clicked)
            button_row.addWidget(self.stop_button)

            self.minmax_label = QLabel()
            self._update_minmax_label()
            button_row.addWidget(self.minmax_label)

            self.reset_button = QPushButton(tr("Reset"))
            self.reset_button.clicked.connect(self._on_reset_clicked)
            button_row.addWidget(self.reset_button)

            if is_complex:
                self.mode_button = QPushButton(tr("Show magnitude"))
                self.mode_button.setCheckable(True)
                self.mode_button.clicked.connect(self._on_mode_clicked)
                button_row.addWidget(self.mode_button)

            layout.addLayout(button_row)

            self.plot = pg.PlotWidget(title=tr("Output signal waveform"))
            self.plot.setLabel("bottom", tr("Sample"))
            self.plot.setLabel("left", tr("Amplitude"), units="%")
            self.plot.setYRange(-55, 75 if is_complex else 55)

            full_scale_pen = pg.mkPen("r", width=1)
            self.plot.addItem(pg.InfiniteLine(pos=50, angle=0, pen=full_scale_pen))
            self.plot.addItem(pg.InfiniteLine(pos=-50, angle=0, pen=full_scale_pen))

            if is_complex:
                self.curve_i = self.plot.plot([], [], pen="c", name="I")
                self.curve_q = self.plot.plot([], [], pen="m", name="Q")
                self.curve_mag = self.plot.plot([], [], pen="y", name="|I+jQ|")
                self.plot.addLegend()
                self.curve_mag.setVisible(False)
            else:
                self.curve = self.plot.plot([], [], pen="y")
            layout.addWidget(self.plot)

            bridge.dataReady.connect(self._on_data, type=Qt.QueuedConnection)

        def _on_stop_clicked(self, checked):
            self._paused = checked
            self.stop_button.setText(tr("Continue") if checked else tr("Stop"))

        def _on_reset_clicked(self):
            self._min_value = None
            self._max_value = None
            self._update_minmax_label()

        def _on_mode_clicked(self, checked):
            self.curve_i.setVisible(not checked)
            self.curve_q.setVisible(not checked)
            self.curve_mag.setVisible(checked)
            self.mode_button.setText(tr("Show I/Q") if checked else tr("Show magnitude"))

        def _update_minmax_label(self):
            min_text = f"{self._min_value:+.1f}%" if self._min_value is not None else "-"
            max_text = f"{self._max_value:+.1f}%" if self._max_value is not None else "-"
            self.minmax_label.setText(tr("Min: {min}   Max: {max}").format(min=min_text, max=max_text))

        def _on_data(self, samples, sample_rate, lo_freq_hz=0.0):
            if bool(np.iscomplexobj(samples)) != self.is_complex:
                return
            block = samples[-TIME_DOMAIN_DISPLAY_SAMPLES:]

            if self.is_complex:
                scaled_i = block.real * 50.0
                scaled_q = block.imag * 50.0
                scaled_mag = np.abs(block) * 50.0
                block_min = float(min(scaled_i.min(), scaled_q.min()))
                block_max = float(max(scaled_i.max(), scaled_q.max(), scaled_mag.max()))
            else:
                scaled = block * 50.0
                block_min = float(scaled.min())
                block_max = float(scaled.max())

            if self._min_value is None or block_min < self._min_value:
                self._min_value = block_min
            if self._max_value is None or block_max > self._max_value:
                self._max_value = block_max
            self._update_minmax_label()

            if self._paused:
                return

            x = np.arange(len(block))
            if self.is_complex:
                self.curve_i.setData(x, scaled_i)
                self.curve_q.setData(x, scaled_q)
                self.curve_mag.setData(x, scaled_mag)
            else:
                self.curve.setData(x, scaled)

else:
    class SpectrumWaterfallWindow:
        def __init__(self, *a, **k):
            raise ImportError(
                "pyqtgraph is not installed - required for the debug windows "
                "(pip install pyqtgraph --break-system-packages)")

    class TimeDomainWindow:
        def __init__(self, *a, **k):
            raise ImportError(
                "pyqtgraph is not installed - required for the debug windows "
                "(pip install pyqtgraph --break-system-packages)")
