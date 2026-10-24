####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import platform
import subprocess

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from backend_factory import create_backend
from i18n import tr

logger = logging.getLogger(__name__)


class SystemWorker(QObject):
    statusMessage = pyqtSignal(str)
    errorOccurred = pyqtSignal(str)
    deviceReady = pyqtSignal(str)
    trackReady = pyqtSignal(object)
    shutdownFinished = pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.backend = create_backend(config)
        self.backend.errorOccurred.connect(self.errorOccurred)
        self.backend.infoMessage.connect(self.statusMessage)

    @pyqtSlot()
    def connect_device(self):
        self.statusMessage.emit(tr("connecting ({device}) ...").format(device=self.config.hwconfig))
        try:
            address = self.backend.connect_device(self.config.host_address)
        except Exception as exc:
            logger.exception("unexpected error while connecting")
            self.errorOccurred.emit(tr("unexpected error while connecting: {error}").format(error=exc))
            return

        if not address:
            self.errorOccurred.emit(tr("device not found / not reachable"))
            return

        if address != self.config.host_address:
            self.config.save_host_address(address)

        self.deviceReady.emit(address)

    @pyqtSlot(dict)
    def configure_track(self, params):
        try:
            ready = self.backend.configure_track(params["sample_rate"], params["center_freq"])
        except Exception as exc:
            logger.exception("unexpected error during device configuration")
            self.errorOccurred.emit(tr("unexpected error during device configuration: {error}").format(error=exc))
            return

        if not ready:
            return

        self.trackReady.emit(self.backend)

    @pyqtSlot(list)
    def reload_live_stations(self, stations):
        reload_method = getattr(self.backend, "reload_live_stations", None)
        if reload_method is None:
            logger.warning("SystemWorker: active backend (%s) does not support "
                           "reload_live_stations() - ignored",
                            type(self.backend).__name__)
            return
        try:
            reload_method(stations)
        except Exception as exc:
            logger.exception("unexpected error while updating the station list")
            self.errorOccurred.emit(tr("station list could not be updated: {error}").format(error=exc))

    @pyqtSlot()
    def shutdown(self):
        try:
            self.backend.shutdown()
        except Exception as exc:
            logger.exception("unexpected error during shutdown")
            self.errorOccurred.emit(tr("unexpected error during shutdown: {error}").format(error=exc))

        if self.config.shutdown_host_os:
            self.statusMessage.emit(tr("shutting down host system ..."))
            self._shutdown_host_os()

        self.shutdownFinished.emit()

    @staticmethod
    def _shutdown_host_os():
        if platform.system() == "Windows":
            subprocess.call(["shutdown", "/s", "/t", "0"])
        else:
            subprocess.call(["shutdown", "-h", "now"])
