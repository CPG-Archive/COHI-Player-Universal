#!/usr/bin/env python3
####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import sys
from pathlib import Path

import i18n
from i18n import tr
from cohi_config_setup import (
    hwconfig_options, program_variant_options, display_variant_options,
    LIVE_VARIANTS, read_file, parse_current_values, replace_yaml_value,
)


def change_fields():
    return [
        (tr("Output hardware"), "hwconfig"),
        (tr("Operating mode"), "program_variant"),
        (tr("Display size"), "display_variant"),
        ("fl2k_upconverter", "fl2k_upconverter"),
    ]


def compute_changes(current, selections):
    changes = []
    for label, key in change_fields():
        old = current.get(key)
        new = selections.get(key, old)
        if str(old) != str(new):
            changes.append((label, key, old, new))
    return changes


def compute_fl2k_upconverter_hint(new_hwconfig, new_program_variant, current_fl2k_upconverter):
    if new_hwconfig == "fl2k" and new_program_variant in LIVE_VARIANTS:
        if current_fl2k_upconverter != "cpp":
            hint = tr("{variant!r} with hwconfig: fl2k requires fl2k_upconverter: cpp "
                      "(current: {current!r}). It will be set automatically when saving."
                      ).format(variant=new_program_variant, current=current_fl2k_upconverter)
            return "cpp", hint
    return current_fl2k_upconverter, None


def compute_stemlab_live_hint(new_hwconfig, new_program_variant, current):
    if new_hwconfig == "stemlab" and new_program_variant == "COHI-Stream":
        has_lo = current.get("stemlab_live_lo_freq") is not None
        has_rate = current.get("stemlab_live_sample_rate") is not None
        if not (has_lo and has_rate):
            return tr("hwconfig: stemlab + program_variant: COHI-Stream additionally requires "
                      "stemlab_live_lo_freq and stemlab_live_sample_rate in config_sdr.yaml - "
                      "this tool does NOT set these values (they depend on the actual station "
                      "list). Please add them manually afterwards.")
    return None


def apply_changes(original_text, selections):
    new_text = original_text
    not_found = []
    for _label, key in change_fields():
        if key not in selections:
            continue
        result = replace_yaml_value(new_text, key, selections[key])
        if result is None:
            not_found.append(key)
            continue
        new_text = result
    return new_text, not_found


try:
    from PyQt5.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QButtonGroup, QMessageBox, QFrame, QScrollArea, QSizePolicy,
    )
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont, QPalette, QColor
    _PYQT5_AVAILABLE = True
except ImportError:
    _PYQT5_AVAILABLE = False

OPTION_BUTTON_STYLE = """
    QPushButton {
        background-color: #3a3a3a;
        color: white;
        border: 2px solid #5a5a5a;
        border-radius: 8px;
        padding: 6px 10px;
        text-align: left;
    }
    QPushButton:checked {
        background-color: #2e7d32;
        border: 2px solid #66bb6a;
        font-weight: bold;
    }
"""
SAVE_BUTTON_STYLE = """
    QPushButton {
        background-color: #1565c0;
        color: white;
        border-radius: 8px;
        padding: 14px;
        font-size: 15px;
        font-weight: bold;
    }
    QPushButton:disabled {
        background-color: #555555;
        color: #999999;
    }
"""
EXIT_BUTTON_STYLE = """
    QPushButton {
        background-color: #4a4a4a;
        color: white;
        border-radius: 8px;
        padding: 14px;
        font-size: 15px;
    }
"""


if _PYQT5_AVAILABLE:
    class ConfigWindow(QWidget):
        def __init__(self, config_path):
            super().__init__()
            self.config_path = config_path
            self.original_text = read_file(config_path)
            self.current = parse_current_values(self.original_text, config_path)

            self.selections = {
                "hwconfig": self.current.get("hwconfig", "fl2k"),
                "program_variant": self.current.get("program_variant", "COHI-Player"),
                "display_variant": self.current.get("display_variant", 1),
            }
            self._button_groups = {}

            self.setWindowTitle(tr("COHI-Player Configuration"))
            self.setWindowFlags(Qt.FramelessWindowHint)
            self.setMinimumSize(360, 220)
            self.resize(400, 260)
            self._build_ui()
            self._update_hints()

        def _build_ui(self):
            outer = QVBoxLayout(self)
            outer.setContentsMargins(8, 8, 8, 8)
            outer.setSpacing(4)

            path_label = QLabel(str(self.config_path))
            path_label.setStyleSheet("color: #aaaaaa; font-size: 10px;")
            outer.addWidget(path_label)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            content_layout = QVBoxLayout(content)
            content_layout.setSpacing(8)

            content_layout.addWidget(self._make_option_section(
                tr("1. Output hardware"), "hwconfig", hwconfig_options()))
            content_layout.addWidget(self._make_option_section(
                tr("2. Operating mode"), "program_variant", program_variant_options()))
            content_layout.addWidget(self._make_option_section(
                tr("3. Display size"), "display_variant", display_variant_options()))

            self.hint_label = QLabel("")
            self.hint_label.setWordWrap(True)
            self.hint_label.setStyleSheet(
                "color: #ffca28; background-color: #3a3320; border-radius: 6px; padding: 8px;")
            self.hint_label.setVisible(False)
            content_layout.addWidget(self.hint_label)

            content_layout.addStretch(1)
            scroll.setWidget(content)
            outer.addWidget(scroll, stretch=1)

            button_row = QHBoxLayout()
            self.exit_button = QPushButton(tr("Quit"))
            self.exit_button.setStyleSheet(EXIT_BUTTON_STYLE)
            self.exit_button.clicked.connect(self.close)
            self.save_button = QPushButton(tr("Save"))
            self.save_button.setStyleSheet(SAVE_BUTTON_STYLE)
            self.save_button.clicked.connect(self._on_save_clicked)
            button_row.addWidget(self.exit_button)
            button_row.addWidget(self.save_button)
            outer.addLayout(button_row)

        def _make_option_section(self, title, key, options):
            frame = QFrame()
            layout = QVBoxLayout(frame)
            layout.setSpacing(2)

            title_label = QLabel(title)
            title_font = QFont()
            title_font.setPointSize(12)
            title_font.setBold(True)
            title_label.setFont(title_font)
            layout.addWidget(title_label)

            group = QButtonGroup(frame)
            group.setExclusive(True)
            buttons = {}
            current_value = self.selections[key]
            for value, description in options:
                btn = QPushButton(f"{value}\n{description}")
                btn.setCheckable(True)
                btn.setMinimumHeight(44)
                btn.setStyleSheet(OPTION_BUTTON_STYLE)
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                if value == current_value:
                    btn.setChecked(True)
                group.addButton(btn)
                buttons[value] = btn
                layout.addWidget(btn)
                btn.clicked.connect(lambda _checked, k=key, v=value: self._on_option_selected(k, v))

            self._button_groups[key] = buttons
            return frame

        def _on_option_selected(self, key, value):
            self.selections[key] = value
            self._update_hints()

        def _update_hints(self):
            _new_fl2k, fl2k_hint = compute_fl2k_upconverter_hint(
                self.selections["hwconfig"], self.selections["program_variant"],
                self.current.get("fl2k_upconverter", "ffmpeg"))
            stemlab_hint = compute_stemlab_live_hint(
                self.selections["hwconfig"], self.selections["program_variant"], self.current)

            hints = [h for h in (fl2k_hint, stemlab_hint) if h]
            if hints:
                self.hint_label.setText("\n\n".join(hints))
                self.hint_label.setVisible(True)
            else:
                self.hint_label.setVisible(False)

        def _on_save_clicked(self):
            new_fl2k_upconverter, _hint = compute_fl2k_upconverter_hint(
                self.selections["hwconfig"], self.selections["program_variant"],
                self.current.get("fl2k_upconverter", "ffmpeg"))
            full_selections = dict(self.selections)
            full_selections["fl2k_upconverter"] = new_fl2k_upconverter

            changes = compute_changes(self.current, full_selections)
            if not changes:
                QMessageBox.information(self, tr("No changes"),
                                         tr("No changes were made."))
                return

            summary = "\n".join(f"  {label}: {old}  ->  {new}" for label, _key, old, new in changes)
            stemlab_hint = compute_stemlab_live_hint(
                self.selections["hwconfig"], self.selections["program_variant"], self.current)
            message = tr("The following changes will be saved:") + f"\n\n{summary}"
            if stemlab_hint:
                message += "\n\n" + tr("Note:") + f"\n{stemlab_hint}"
            message += "\n\n" + tr("Save now?")

            reply = QMessageBox.question(
                self, tr("Save changes?"), message,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

            new_text, not_found = apply_changes(self.original_text, full_selections)
            backup_path = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            try:
                backup_path.write_text(self.original_text, encoding="utf-8")
                self.config_path.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                QMessageBox.critical(self, tr("Error while saving"), str(exc))
                return

            info = tr("{file} has been updated.\nBackup copy: {backup}").format(
                file=self.config_path, backup=backup_path)
            if not_found:
                info += "\n\n" + tr("Warning: the following keys were not found in the file and "
                                     "must be added manually: {keys}").format(keys=", ".join(not_found))
            QMessageBox.information(self, tr("Saved"), info)

            self.original_text = new_text
            self.current = parse_current_values(new_text, self.config_path)


def _run_gui(config_path):
    app = QApplication(sys.argv)
    i18n.install_qt_translator(app)
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.Window, QColor(30, 30, 30))
    dark_palette.setColor(QPalette.WindowText, QColor(255, 255, 255))
    app.setPalette(dark_palette)

    window = ConfigWindow(config_path)

    available = app.desktop().availableGeometry()
    window.setGeometry(available)

    window.show()
    sys.exit(app.exec_())


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config_sdr.yaml")
    i18n.init_from_config(str(config_path))

    if not config_path.exists():
        print(tr("{file} was not found.").format(file=config_path))
        print(tr("Please run this in the COHI-Player installation directory, "
                 "or pass the path as an argument:"))
        print("  python3 cohi_config_gui.py /path/to/config_sdr.yaml")
        sys.exit(1)

    if not _PYQT5_AVAILABLE:
        print(tr("Error: PyQt5 is not installed."))
        print(tr("Install with: pip install PyQt5 --break-system-packages"))
        print(tr("(For the terminal version without a graphical interface see cohi_config_setup.py)"))
        sys.exit(1)

    _run_gui(config_path)


if __name__ == "__main__":
    main()
