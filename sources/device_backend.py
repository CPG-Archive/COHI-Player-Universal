####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

from PyQt5.QtCore import QObject, pyqtSignal


class DeviceBackend(QObject):
    errorOccurred = pyqtSignal(str)
    infoMessage = pyqtSignal(str)

    default_gain = 0.05

    @property
    def silence_required_during_mute(self):
        return True

    def identify(self):
        raise NotImplementedError

    def connect_device(self, configured_address):
        raise NotImplementedError

    def configure_track(self, sample_rate, center_freq):
        raise NotImplementedError

    def encode_samples(self, samples, gain):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError
