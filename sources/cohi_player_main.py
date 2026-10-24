####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
import sys
import threading

import yaml
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox,
                              QTableWidgetItem)

import live_streams
from COHI_player_UI import Ui_MainWindow
from backend_factory import backend_display_name
from i18n import install_qt_translator, tr
from constants import (DEFAULT_LOG_MODE, JUNKSIZE, LOG_FILE_NAME, LOG_MODE_APPEND,
                        LOG_MODE_SESSION, TIMESCALER, VALID_LOG_MODES, normalize_log_mode)
from player_config import ConfigError, PlayerConfig
from playback_worker import PlaybackWorker
from station_edit_dialog import StationEditDialog
from system_worker import SystemWorker

logger = logging.getLogger(__name__)

COHI_STREAM_NOMINAL_SAMPLE_RATE = 1_250_000
COHI_STREAM_NOMINAL_CENTER_FREQ = 1_000_000


def _make_status_circle_pixmap(color, size=14):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return pixmap


def format_hhmmss(total_seconds):
    total_seconds = max(0, int(total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def chunks_to_seconds(chunk_count):
    junks_per_second = TIMESCALER / JUNKSIZE * 32
    return chunk_count / junks_per_second


def _peek_log_mode():
    try:
        with open(PlayerConfig.CONFIG_FILE, "r") as stream:
            raw_config = yaml.safe_load(stream) or {}
    except OSError:
        return DEFAULT_LOG_MODE
    log_mode = normalize_log_mode(raw_config.get("log_mode", DEFAULT_LOG_MODE))
    return log_mode if log_mode in VALID_LOG_MODES else DEFAULT_LOG_MODE


def setup_logging(log_mode):
    handlers = [logging.StreamHandler()]
    if log_mode == LOG_MODE_SESSION:
        handlers.append(logging.FileHandler(LOG_FILE_NAME, mode="w"))
    elif log_mode == LOG_MODE_APPEND:
        handlers.append(logging.FileHandler(LOG_FILE_NAME, mode="a"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )


class MainWindow(QMainWindow, Ui_MainWindow):
    seekRequested = pyqtSignal(str, int, int)
    liveStationsChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.config = PlayerConfig()
        try:
            self.config.load()
        except ConfigError as exc:
            self._fatal_error(str(exc))
            return

        self.setupUi(self, display_variant=self.config.display_variant,
                     program_variant=self.config.program_variant)

        self.rf_backend_info.setText(tr("RF backend:") + f"\n{backend_display_name(self.config)}")
        self.pushButton_shutdown.setText(tr("Shutdown") if self.config.hwconfig == "stemlab" else tr("Quit"))

        if self.config.program_variant == "COHI-Stream":
            self._init_cohi_stream()
            return

        if self.config.program_variant == "COHI-Player+Stream":
            self._init_combined()
            return

        try:
            self.config.scan_tracks()
        except ConfigError as exc:
            self._fatal_error(str(exc))
            return

        if self.config.track_count == 0:
            self._fatal_error(tr("no WAV files found, program will exit"))
            return

        self._current_track_index = 0
        self._slider_being_dragged = False
        self._ignore_next_position_update = False

        self.playing_wav.setText(tr("connecting ({device}) ...").format(device=self.config.hwconfig))
        self.play_time.setText("")
        self.play_total.setText("")

        if self.config.display_variant == 2:
            self._populate_track_table()

        try:
            self._setup_threads()
        except ValueError as exc:
            self._fatal_error(str(exc))
            return
        self._connect_signals()

        self.system_thread.start()
        self.playback_thread.start(QThread.HighPriority)

    def _init_cohi_stream(self):
        self.title.setText("COHI-Stream")
        self._init_live_editor_widgets()
        self._setup_live_broadcast()

    def _init_live_editor_widgets(self):
        self._status_pixmap_green = _make_status_circle_pixmap(QColor(0, 220, 0))
        self._status_pixmap_red = _make_status_circle_pixmap(QColor(220, 0, 0))

        startup_path = live_streams.read_last_stations_path() or self.config.live_stations_file
        self._current_stations_path = None
        self._live_stations = []
        self._stations_dirty = False
        if startup_path:
            try:
                self._live_stations = live_streams.load_station_list(startup_path)
                self._current_stations_path = startup_path
            except OSError as exc:
                logger.warning("COHI-Stream: station list could not be loaded at startup: %s", exc)
        self._refresh_stream_table()
        self._update_playing_live_streams_label()
        self.config.live_stations_file = self._current_stations_path

        self.pushButton_load.clicked.connect(self._on_load_stations_clicked)
        self.pushButton_save.clicked.connect(self._on_save_stations_clicked)
        self.pushButton_edit.clicked.connect(self._on_edit_station_clicked)
        self.table_streams.cellDoubleClicked.connect(
            lambda row, _column: self._on_edit_station_clicked())

    def _init_combined(self):
        self.title_live.setText("COHI-Stream")
        self.rf_backend_info_live.setText(tr("RF backend:") + f"\n{backend_display_name(self.config)}")

        try:
            self.config.scan_tracks()
        except ConfigError as exc:
            self._fatal_error(str(exc))
            return

        if self.config.track_count == 0:
            self._fatal_error(tr("no WAV files found, program will exit"))
            return

        self._current_track_index = 0
        self._slider_being_dragged = False
        self._ignore_next_position_update = False

        self.playing_wav.setText(tr("connecting ({device}) ...").format(device=self.config.hwconfig))
        self.play_time.setText("")
        self.play_total.setText("")

        if self.config.display_variant == 2:
            self._populate_track_table()

        self._init_live_editor_widgets()

        try:
            self._setup_threads()
        except ValueError as exc:
            self._fatal_error(str(exc))
            return
        self._connect_signals()
        self.liveStationsChanged.connect(self.system_worker.reload_live_stations)

        if hasattr(self, "checkBox_live_toggle"):
            self.checkBox_live_toggle.setChecked(True)
            self.checkBox_live_toggle.toggled.connect(self._on_live_output_toggled)

        if hasattr(self, "pushButton_shift_right"):
            self.pushButton_shift_right.clicked.connect(self._on_shift_right_clicked)
        if hasattr(self, "pushButton_shift_left"):
            self.pushButton_shift_left.clicked.connect(self._on_shift_left_clicked)

        if hasattr(self, "playing_live_streams"):
            self.playing_live_streams.setText(tr("connecting ({device}) ...").format(device=self.config.hwconfig))

        self.system_thread.start()
        self.playback_thread.start(QThread.HighPriority)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_stream_status)
        self._status_timer.start(2000)

    def _on_shift_right_clicked(self):
        self.central_scroll_area.horizontalScrollBar().setValue(480)

    def _on_shift_left_clicked(self):
        self.central_scroll_area.horizontalScrollBar().setValue(0)

    def _on_live_output_toggled(self, checked):
        if not hasattr(self, "system_worker"):
            return
        self.liveStationsChanged.emit(list(self._live_stations) if checked else [])

    def _setup_live_broadcast(self):
        self.system_thread = QThread(self)
        self.system_worker = SystemWorker(self.config)
        self.system_worker.moveToThread(self.system_thread)
        self._early_setup_debug()

        self.system_thread.started.connect(self.system_worker.connect_device)
        if self.config.hwconfig == "stemlab":
            live_params = {"sample_rate": self.config.stemlab_live_sample_rate,
                           "center_freq": self.config.stemlab_live_lo_freq}
        else:
            live_params = {"sample_rate": COHI_STREAM_NOMINAL_SAMPLE_RATE,
                           "center_freq": COHI_STREAM_NOMINAL_CENTER_FREQ}
        self.system_worker.deviceReady.connect(
            lambda address: self.system_worker.configure_track(live_params))
        self.system_worker.deviceReady.connect(self._on_device_ready)

        self.system_worker.statusMessage.connect(self._on_status_message)
        self.system_worker.errorOccurred.connect(self._on_error)
        self.liveStationsChanged.connect(self.system_worker.reload_live_stations)

        self.pushButton_shutdown.clicked.connect(self._on_shutdown_clicked)
        self.system_worker.shutdownFinished.connect(self._on_shutdown_finished)

        if hasattr(self, "playing_live_streams"):
            self.playing_live_streams.setText(tr("connecting ({device}) ...").format(device=self.config.hwconfig))

        self.system_thread.start()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_stream_status)
        self._status_timer.start(2000)

    def _refresh_stream_status(self):
        backend = getattr(self.system_worker, "backend", None)
        backend_stations = getattr(backend, "_live_stations", None)
        if not backend_stations:
            return
        starved_streak = getattr(backend, "_port_starved_streak", {})
        status_by_port = {
            station.udp_port: (station.process is not None and station.process.poll() is None
                                and starved_streak.get(station.udp_port, 0) == 0)
            for station in backend_stations
        }

        for row, station in enumerate(self._live_stations):
            is_running = status_by_port.get(station.udp_port)
            if is_running is None:
                continue
            label = self.table_streams.cellWidget(row, 0)
            if label is not None:
                label.setPixmap(self._status_pixmap_green if is_running else self._status_pixmap_red)

    def _notify_live_stations_changed(self):
        if hasattr(self, "system_worker"):
            self.liveStationsChanged.emit(list(self._live_stations))

    def _refresh_stream_table(self):
        self._live_stations.sort(key=lambda station: station.frequency_hz)
        self.table_streams.setRowCount(len(self._live_stations))
        for row, station in enumerate(self._live_stations):
            self.table_streams.setItem(row, 0, QTableWidgetItem(""))
            status_label = QLabel()
            status_label.setPixmap(self._status_pixmap_green)
            status_label.setAlignment(Qt.AlignCenter)
            self.table_streams.setCellWidget(row, 0, status_label)

            freq_item = QTableWidgetItem(f"{station.frequency_hz / 1000:.1f}")
            freq_item.setTextAlignment(Qt.AlignCenter)
            bw_item = QTableWidgetItem(f"{station.bandwidth_hz / 1000:.1f}")
            bw_item.setTextAlignment(Qt.AlignCenter)

            self.table_streams.setItem(row, 1, freq_item)
            self.table_streams.setItem(row, 2, bw_item)
            self.table_streams.setItem(row, 3, QTableWidgetItem(station.name))

    def _update_playing_live_streams_label(self):
        if not hasattr(self, "playing_live_streams"):
            return
        name = os.path.basename(self._current_stations_path) if self._current_stations_path else "-"
        self.playing_live_streams.setText(tr("currently playing live streams: {name}").format(name=name))

    def _show_sized_file_dialog(self, mode, caption, name_filter, selected_filter=None):
        dialog = QFileDialog(self, caption, "", name_filter)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        if mode == "open":
            dialog.setFileMode(QFileDialog.ExistingFile)
            dialog.setAcceptMode(QFileDialog.AcceptOpen)
        else:
            dialog.setFileMode(QFileDialog.AnyFile)
            dialog.setAcceptMode(QFileDialog.AcceptSave)
        if selected_filter:
            dialog.selectNameFilter(selected_filter)
        if self.config.display_variant == 1:
            dialog.resize(470, 300)
        if dialog.exec_() != QFileDialog.Accepted:
            return ""
        selected = dialog.selectedFiles()
        return selected[0] if selected else ""

    def _on_load_stations_clicked(self):
        path = self._show_sized_file_dialog(
            "open", tr("Load station list"), tr("CSV files (*.csv);;All files (*)"))
        if not path:
            return
        loaded = live_streams.load_station_list(path)
        if not loaded:
            QMessageBox.warning(self, tr("Load station list"),
                                 tr("No valid stations found in {file}.").format(file=path))
            return
        self._live_stations = loaded
        self._current_stations_path = path
        self._stations_dirty = False
        live_streams.write_last_stations_path(path)
        self._refresh_stream_table()
        self._update_playing_live_streams_label()
        self._notify_live_stations_changed()

    def _on_save_stations_clicked(self):
        path = self._show_sized_file_dialog(
            "save", tr("Save station list"), tr("CSV files (*.csv);;All files (*)"),
            selected_filter=tr("CSV files (*.csv)"))
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            live_streams.save_station_list(self._live_stations, path)
        except OSError as exc:
            QMessageBox.warning(self, tr("Save station list"),
                                 tr("Saving failed: {error}").format(error=exc))
            return
        self._current_stations_path = path
        self._stations_dirty = False
        live_streams.write_last_stations_path(path)
        self._update_playing_live_streams_label()

    def _on_edit_station_clicked(self):
        row = self.table_streams.currentRow()
        station = self._live_stations[row] if 0 <= row < len(self._live_stations) else None

        dialog_width = 480 if self.config.display_variant == 1 else 560
        dialog = StationEditDialog(self, station, width=dialog_width)
        dialog.exec_()
        action = dialog.result_action()

        if action == StationEditDialog.ACTION_ADD:
            self._live_stations.append(dialog.get_station())
            self._stations_dirty = True
            self._refresh_stream_table()
            self._notify_live_stations_changed()
        elif action == StationEditDialog.ACTION_MODIFY:
            if station is None:
                QMessageBox.information(self, tr("Edit station"),
                                         tr("Please select a row in the table first."))
            else:
                self._live_stations[row] = dialog.get_station()
                self._stations_dirty = True
                self._refresh_stream_table()
                self._notify_live_stations_changed()
        elif action == StationEditDialog.ACTION_DELETE:
            if station is None:
                QMessageBox.information(self, tr("Delete station"),
                                         tr("Please select a row in the table first."))
            else:
                del self._live_stations[row]
                self._stations_dirty = True
                self._refresh_stream_table()
                self._notify_live_stations_changed()

    def _fatal_error(self, message):
        logger.critical(message)
        QMessageBox.critical(self, tr("FATAL ERROR"), message)
        sys.exit(1)

    def _setup_threads(self):
        self.playback_thread = QThread(self)
        self.playback_worker = PlaybackWorker(self.config)
        self.playback_worker.moveToThread(self.playback_thread)

        self.system_thread = QThread(self)
        self.system_worker = SystemWorker(self.config)
        self.system_worker.moveToThread(self.system_thread)

        self.system_thread.started.connect(self.system_worker.connect_device)
        self.system_worker.deviceReady.connect(self.playback_worker.start)
        self._early_setup_debug()

    def _connect_signals(self):
        self.pushButton_prev.clicked.connect(self.playback_worker.previous_track)
        self.pushButton_next.clicked.connect(self.playback_worker.next_track)
        self.pushButton_restart.clicked.connect(self.playback_worker.restart_track)
        self.pushButton_pause.clicked.connect(self.playback_worker.pause)
        self.pushButton_cont.clicked.connect(self.playback_worker.resume)
        if self.config.display_variant == 2:
            self.table_tracks.cellClicked.connect(
                lambda row, _column: self.playback_worker.select_track(row))
        if self.config.program_variant == "COHI-Player":
            self.checkBox_wav_mute.setVisible(False)
        else:
            self.checkBox_wav_mute.setChecked(True)
            self.checkBox_wav_mute.toggled.connect(
                lambda checked: self.playback_worker.set_wav_muted(not checked))
        self.seekRequested.connect(self.playback_worker.seek)

        self.ScrollBar_playtime.sliderPressed.connect(self._on_slider_pressed)
        self.ScrollBar_playtime.sliderReleased.connect(self._on_slider_released)

        self.pushButton_shutdown.clicked.connect(self._on_shutdown_clicked)
        self.playback_worker.shutdownComplete.connect(self.system_worker.shutdown)

        self.system_worker.trackReady.connect(self.playback_worker.begin_track)
        self.playback_worker.deviceConfigNeeded.connect(self.system_worker.configure_track)

        self.system_worker.statusMessage.connect(self._on_status_message)
        self.system_worker.errorOccurred.connect(self._on_error)
        self.system_worker.deviceReady.connect(self._on_device_ready)
        self.system_worker.shutdownFinished.connect(self._on_shutdown_finished)

        self.playback_worker.fileChanged.connect(self._on_file_changed)
        self.playback_worker.positionChanged.connect(self._on_position_changed)
        self.playback_worker.stateChanged.connect(self._on_state_changed)
        self.playback_worker.errorOccurred.connect(self._on_error)
        self.playback_worker.trackChanged.connect(self._on_track_changed)

    def _on_status_message(self, message):
        logger.info(message)
        if hasattr(self, "playing_wav"):
            self.playing_wav.setText(message)
        if hasattr(self, "playing_live_streams"):
            self.playing_live_streams.setText(message)
            self._restore_playing_live_streams_timer().start(3000)

    def _restore_playing_live_streams_timer(self):
        if not hasattr(self, "_playing_live_streams_timer"):
            self._playing_live_streams_timer = QTimer(self)
            self._playing_live_streams_timer.setSingleShot(True)
            self._playing_live_streams_timer.timeout.connect(self._update_playing_live_streams_label)
        return self._playing_live_streams_timer

    def _on_error(self, message):
        logger.error(message)
        if hasattr(self, "playing_wav"):
            self.playing_wav.setText(tr("error: {message}").format(message=message))
        if hasattr(self, "playing_live_streams"):
            self.playing_live_streams.setText(tr("error: {message}").format(message=message))
        if tr("not found") in message and (
                "cohi_native_upconverter" in message or "cohi_stemlab_live_upconverter" in message):
            QMessageBox.critical(
                self, tr("C++ tool not found"),
                f"{message}\n\n"
                + tr("Please run 'make' in the installation directory to compile the "
                     "required C++ tools (see Makefile)."))

    def _on_device_ready(self, address):
        text = tr("connected ({device}): {address}").format(device=self.config.hwconfig, address=address)
        if hasattr(self, "playing_wav"):
            self.playing_wav.setText(text)
        if hasattr(self, "playing_live_streams"):
            self.playing_live_streams.setText(text)
            self._restore_playing_live_streams_timer().start(3000)

        QTimer.singleShot(2000, self._setup_debug_data_feed)

    def _early_setup_debug(self):
        self._debug_udp_port = None
        self._debug_bridge = None
        self._debug_spectrum_window_instance = None
        self._debug_timedomain_window_instance = None
        self._debug_data_feed_initialized = False

        if hasattr(self, "pushButton_debug_spectrum"):
            self.pushButton_debug_spectrum.setVisible(self.config.debug_spectrum_window)
            self.pushButton_debug_spectrum.setEnabled(self.config.debug_spectrum_window)
            if self.config.debug_spectrum_window:
                self.pushButton_debug_spectrum.clicked.connect(self._on_debug_spectrum_clicked)
        if hasattr(self, "pushButton_debug_timedomain"):
            self.pushButton_debug_timedomain.setVisible(self.config.debug_timedomain_window)
            self.pushButton_debug_timedomain.setEnabled(self.config.debug_timedomain_window)
            if self.config.debug_timedomain_window:
                self.pushButton_debug_timedomain.clicked.connect(self._on_debug_timedomain_clicked)

        if not (self.config.debug_spectrum_window or self.config.debug_timedomain_window):
            return

        from debug_windows import DebugDataBridge
        self._debug_bridge = DebugDataBridge()

        if self.config.hwconfig == "fl2k":
            from debug_windows import _make_debug_udp_socket_free_port
            self._debug_udp_port = _make_debug_udp_socket_free_port()
            self.system_worker.backend.set_debug_udp_port(self._debug_udp_port)

    def _setup_debug_data_feed(self):
        if self._debug_data_feed_initialized:
            return
        if self._debug_bridge is None:
            return
        self._debug_data_feed_initialized = True

        try:
            from debug_windows import fl2k_udp_listener_loop
        except ImportError as exc:
            logger.error("debug displays requested but import failed: %s", exc)
            return

        if self.config.hwconfig == "stemlab":
            backend = self.system_worker.backend
            params = backend._current_params
            sample_rate = float(params[0]) if params else 2_500_000.0
            backend.set_debug_tap(
                lambda iq, lo_freq_hz: self._debug_bridge.dataReady.emit(iq, sample_rate, lo_freq_hz))
        elif self.config.hwconfig == "fl2k" and self._debug_udp_port:
            sample_rate = float(self.system_worker.backend.target_rate or 10_000_000.0)
            self._debug_stop_event = threading.Event()
            threading.Thread(
                target=fl2k_udp_listener_loop,
                args=(self._debug_udp_port, self._debug_bridge, sample_rate, self._debug_stop_event),
                name="fl2k-debug-udp", daemon=True).start()

    def _on_debug_spectrum_clicked(self):
        if self._debug_bridge is None:
            return
        if self._debug_spectrum_window_instance is None:
            from debug_windows import SpectrumWaterfallWindow
            is_complex = (self.config.hwconfig == "stemlab")
            self._debug_spectrum_window_instance = SpectrumWaterfallWindow(self._debug_bridge, is_complex)
        self._debug_spectrum_window_instance.show()
        self._debug_spectrum_window_instance.raise_()
        self._debug_spectrum_window_instance.activateWindow()

    def _on_debug_timedomain_clicked(self):
        if self._debug_bridge is None:
            return
        if self._debug_timedomain_window_instance is None:
            from debug_windows import TimeDomainWindow
            is_complex = (self.config.hwconfig == "stemlab")
            self._debug_timedomain_window_instance = TimeDomainWindow(self._debug_bridge, is_complex)
        self._debug_timedomain_window_instance.show()
        self._debug_timedomain_window_instance.raise_()
        self._debug_timedomain_window_instance.activateWindow()

    def _on_shutdown_finished(self):
        self.close()

    def _on_file_changed(self, file_path):
        source_index = self.config.source_index_for_path(file_path)
        prefix = f"({source_index}) " if source_index else ""
        self.playing_wav.setText(tr("currently playing WAV file: {name}").format(
            name=f"{prefix}{os.path.basename(file_path)}"))

    def _on_track_changed(self, track_index):
        self._current_track_index = track_index
        total_chunks = self._track_total_chunks(track_index)
        self.play_total.setText(tr("total playing time: ") + format_hhmmss(chunks_to_seconds(total_chunks)))
        if self.config.display_variant == 2:
            self._highlight_current_track(track_index)

    def _on_position_changed(self, chunk_count):
        if self._slider_being_dragged or self._ignore_next_position_update:
            self._ignore_next_position_update = False
            return

        self.play_time.setText(tr("current playing time: ") + format_hhmmss(chunks_to_seconds(chunk_count)))

        total_chunks = self._track_total_chunks(self._current_track_index)
        if total_chunks > 0:
            progress_permille = int(chunk_count / total_chunks * 1000)
            self.ScrollBar_playtime.setProperty("value", progress_permille)

    def _on_state_changed(self, state):
        pass

    def _track_total_chunks(self, track_index):
        segments = self.config.tracks[track_index]
        return segments[-1].stop_byte / JUNKSIZE

    def _populate_track_table(self):
        self.table_tracks.setRowCount(self.config.track_count)
        for row, segments in enumerate(self.config.tracks):
            start_path = segments[0].file_path
            filename = os.path.basename(start_path)
            if len(segments) > 1:
                filename += " ..."

            total_chunks = segments[-1].stop_byte / JUNKSIZE
            duration = format_hhmmss(chunks_to_seconds(total_chunks))

            source_index = self.config.source_index_for_path(start_path)
            source_text = f"({source_index})" if source_index else ""
            if source_index:
                relative_dir = os.path.dirname(
                    os.path.relpath(start_path, self.config.media_paths[source_index - 1]))
            else:
                relative_dir = os.path.dirname(start_path)

            duration_item = QTableWidgetItem(duration)
            duration_item.setTextAlignment(Qt.AlignCenter)
            source_item = QTableWidgetItem(source_text)
            source_item.setTextAlignment(Qt.AlignCenter)

            self.table_tracks.setItem(row, 0, QTableWidgetItem(filename))
            self.table_tracks.setItem(row, 1, duration_item)
            self.table_tracks.setItem(row, 2, source_item)
            self.table_tracks.setItem(row, 3, QTableWidgetItem(relative_dir))

    def _highlight_current_track(self, track_index):
        highlight_bg = QColor(200, 200, 200)
        highlight_fg = QColor(0, 0, 0)
        default_bg = QColor(64, 64, 64)
        default_fg = QColor(255, 255, 255)
        for row in range(self.table_tracks.rowCount()):
            is_current = (row == track_index)
            bg = highlight_bg if is_current else default_bg
            fg = highlight_fg if is_current else default_fg
            for col in range(self.table_tracks.columnCount()):
                item = self.table_tracks.item(row, col)
                if item is not None:
                    item.setBackground(bg)
                    item.setForeground(fg)

    def _on_slider_pressed(self):
        self._slider_being_dragged = True

    def _on_slider_released(self):
        self._slider_being_dragged = False
        self._ignore_next_position_update = True
        self._jump_to_slider_position()

    def _jump_to_slider_position(self):
        segments = self.config.tracks[self._current_track_index]
        total_chunks = self._track_total_chunks(self._current_track_index)

        relative_position = self.ScrollBar_playtime.value()
        target_chunk = int(relative_position / 1000 * total_chunks)

        for segment in segments:
            segment_start_chunk = segment.start_byte / JUNKSIZE
            segment_stop_chunk = segment.stop_byte / JUNKSIZE
            if segment_start_chunk <= target_chunk < segment_stop_chunk:
                offset_in_segment = int(target_chunk - segment_start_chunk)
                self.seekRequested.emit(segment.file_path, offset_in_segment, int(segment_start_chunk))
                return

        logger.warning("seek target outside the track: %s", relative_position)

    def _on_shutdown_clicked(self):
        answer = QMessageBox.question(
            self, tr("Confirm shutdown"),
            tr("Do you really want to shut down the system?"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        if hasattr(self, "playback_worker"):
            self.playback_worker.request_stop(full_shutdown=True)
        else:
            self.system_worker.shutdown()

    def closeEvent(self, event):
        if getattr(self, "_stations_dirty", False):
            answer = QMessageBox.question(
                self, tr("Unsaved changes"),
                tr("The live station list has been modified and not saved yet. "
                   "Quit anyway?"),
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save:
                self._on_save_stations_clicked()
                if self._stations_dirty:
                    event.ignore()
                    return

        if not hasattr(self, "system_worker"):
            event.accept()
            return

        if hasattr(self, "playback_worker"):
            self.playback_worker.request_stop()
            self.playback_thread.quit()
            self.playback_thread.wait(2000)

        try:
            self.system_worker.backend.close()
        except Exception:
            logger.exception("error while shutting down the backend during closeEvent")

        self.system_thread.quit()
        self.system_thread.wait(2000)

        event.accept()


if __name__ == "__main__":
    setup_logging(_peek_log_mode())
    app = QApplication(sys.argv)
    install_qt_translator(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
