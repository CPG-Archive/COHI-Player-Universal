####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

from PyQt5.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox, QPushButton

from i18n import tr
from live_streams import LiveStation


class StationEditDialog(QDialog):
    ACTION_CANCEL = 0
    ACTION_ADD = 1
    ACTION_MODIFY = 2
    ACTION_DELETE = 3

    def __init__(self, parent, station=None, width=560):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit station"))
        self._result_action = self.ACTION_CANCEL
        self.setFixedWidth(width)

        layout = QFormLayout(self)
        self.freq_edit = QLineEdit(f"{station.frequency_hz / 1000:.1f}" if station else "")
        self.bw_edit = QLineEdit(f"{station.bandwidth_hz / 1000:.1f}" if station else "")
        self.name_edit = QLineEdit(station.name if station else "")
        self.url_edit = QLineEdit(station.url if station else "")
        layout.addRow(tr("Frequency (kHz):"), self.freq_edit)
        layout.addRow(tr("Bandwidth (kHz):"), self.bw_edit)
        layout.addRow(tr("Station name:"), self.name_edit)
        layout.addRow("URL:", self.url_edit)

        button_row = QHBoxLayout()
        btn_add = QPushButton(tr("Add"))
        btn_modify = QPushButton(tr("Modify"))
        btn_delete = QPushButton(tr("Delete"))
        btn_cancel = QPushButton(tr("Cancel"))
        for button in (btn_add, btn_modify, btn_delete, btn_cancel):
            button_row.addWidget(button)
        layout.addRow(button_row)

        btn_add.clicked.connect(lambda: self._finish(self.ACTION_ADD))
        btn_modify.clicked.connect(lambda: self._finish(self.ACTION_MODIFY))
        btn_delete.clicked.connect(lambda: self._finish(self.ACTION_DELETE))
        btn_cancel.clicked.connect(lambda: self._finish(self.ACTION_CANCEL))

    def _finish(self, action):
        if action in (self.ACTION_ADD, self.ACTION_MODIFY):
            try:
                self.get_station()
            except ValueError as exc:
                QMessageBox.warning(self, tr("Invalid input"), str(exc))
                return

        self._result_action = action
        if action == self.ACTION_CANCEL:
            self.reject()
        else:
            self.accept()

    def result_action(self):
        return self._result_action

    def get_station(self):
        try:
            freq_khz = float(self.freq_edit.text().strip().replace(",", "."))
            bw_khz = float(self.bw_edit.text().strip().replace(",", "."))
        except ValueError:
            raise ValueError(tr("Frequency and bandwidth must be valid numbers (e.g. 556.0)"))
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name:
            raise ValueError(tr("Station name must not be empty"))
        if not url:
            raise ValueError(tr("URL must not be empty"))
        return LiveStation(freq_khz * 1000.0, bw_khz * 1000.0, name, url, 0)
