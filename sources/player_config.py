####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import time
from pathlib import Path

import yaml

from i18n import normalize_language, tr
from constants import DEFAULT_LOG_MODE, VALID_LOG_MODES, normalize_log_mode
from wav_tools import WavHeaderTools
from yaml_edit_utils import replace_yaml_value

logger = logging.getLogger(__name__)

VALID_PROGRAM_VARIANTS = ("COHI-Player", "COHI-Stream", "COHI-Player+Stream")
DEFAULT_PROGRAM_VARIANT = "COHI-Player"

VALID_DISPLAY_VARIANTS = (1, 2)
DEFAULT_DISPLAY_VARIANT = 1


class ConfigError(Exception):
    pass


class TrackSegment:
    __slots__ = ("file_path", "start_byte", "stop_byte")

    def __init__(self, file_path, start_byte, stop_byte):
        self.file_path = file_path
        self.start_byte = start_byte
        self.stop_byte = stop_byte


class PlayerConfig:
    CONFIG_FILE = "config_sdr.yaml"
    MEDIA_MOUNT_WAIT_S = 2

    def __init__(self):
        self.host_address = ""
        self.shutdown_host_os = False
        self.autovol = 1.0
        self.agc_interval = 0
        self.hwconfig = "stemlab"
        self.log_mode = DEFAULT_LOG_MODE
        self.fl2k_tool_path = None
        self.ffmpeg_tool_path = None
        self.fl2k_upconverter = "ffmpeg"
        self.fl2k_cpp_upconverter_path = None
        self.program_variant = "COHI-Player"
        self.display_variant = 1
        self.debug_spectrum_window = False
        self.debug_timedomain_window = False
        self.live_enabled = False
        self.live_stations_file = None
        self.stemlab_live_lo_freq = None
        self.stemlab_live_sample_rate = None
        self.media_paths = []
        self.tracks = []
        self._raw_config = {}

    @property
    def track_count(self):
        return len(self.tracks)

    def load(self):
        time.sleep(self.MEDIA_MOUNT_WAIT_S)

        try:
            with open(self.CONFIG_FILE, "r") as stream:
                self._raw_config = yaml.safe_load(stream) or {}
        except OSError as exc:
            raise ConfigError(tr("{file} could not be read: {error}").format(
                file=self.CONFIG_FILE, error=exc)) from exc

        self.media_paths = [
            value for key, value in self._raw_config.items()
            if key.startswith("filename") and value
        ]
        if not self.media_paths:
            raise ConfigError(tr("no WAV directories (filename/filename2/...) configured in {file}").format(
                file=self.CONFIG_FILE))

        self.host_address = self._raw_config.get("address", "")
        self.shutdown_host_os = bool(self._raw_config.get("shutdown", False))
        try:
            autovol_raw = self._raw_config.get("autovol", 1.0)
            if isinstance(autovol_raw, str):
                autovol_raw = autovol_raw.replace(",", ".")
            self.autovol = float(autovol_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(tr("invalid value for {key} in {file}: {error}").format(
                key="autovol", file=self.CONFIG_FILE, error=exc)) from exc

        try:
            agc_interval_raw = self._raw_config.get("agc_interval", 0)
            if isinstance(agc_interval_raw, str):
                agc_interval_raw = agc_interval_raw.replace(",", ".")
            self.agc_interval = float(agc_interval_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(tr("invalid value for {key} in {file}: {error}").format(
                key="agc_interval", file=self.CONFIG_FILE, error=exc)) from exc
        if self.agc_interval < 0:
            raise ConfigError(tr("{key} in {file} must not be negative").format(
                key="agc_interval", file=self.CONFIG_FILE))

        self.program_variant = self._raw_config.get("program_variant", DEFAULT_PROGRAM_VARIANT)
        if self.program_variant not in VALID_PROGRAM_VARIANTS:
            raise ConfigError(tr("invalid value for {key} in {file}: '{value}' (valid: {valid})").format(
                key="program_variant", file=self.CONFIG_FILE, value=self.program_variant,
                valid=", ".join(VALID_PROGRAM_VARIANTS)))

        try:
            self.display_variant = int(self._raw_config.get("display_variant", DEFAULT_DISPLAY_VARIANT))
        except (TypeError, ValueError) as exc:
            raise ConfigError(tr("invalid value for {key} in {file}: {error}").format(
                key="display_variant", file=self.CONFIG_FILE, error=exc)) from exc
        if self.display_variant not in VALID_DISPLAY_VARIANTS:
            raise ConfigError(tr("invalid value for {key} in {file}: '{value}' (valid: {valid})").format(
                key="display_variant", file=self.CONFIG_FILE, value=self.display_variant,
                valid=", ".join(str(v) for v in VALID_DISPLAY_VARIANTS)))

        self.debug_spectrum_window = bool(self._raw_config.get("debug_spectrum_window", False))
        self.debug_timedomain_window = bool(self._raw_config.get("debug_timedomain_window", False))
        if self.display_variant != 2 and (self.debug_spectrum_window or self.debug_timedomain_window):
            logger.info("debug_spectrum_window/debug_timedomain_window only take effect with "
                        "display_variant: 2 - with display_variant: %s they have no effect.",
                        self.display_variant)
            self.debug_spectrum_window = False
            self.debug_timedomain_window = False

        self.hwconfig = self._raw_config.get("hwconfig", "stemlab")
        self.fl2k_tool_path = self._raw_config.get("fl2k_path")
        self.ffmpeg_tool_path = self._raw_config.get("ffmpeg_path")
        self.fl2k_upconverter = self._raw_config.get("fl2k_upconverter", "ffmpeg")
        self.fl2k_cpp_upconverter_path = self._raw_config.get("fl2k_cpp_upconverter_path")
        self.stemlab_live_upconverter_path = self._raw_config.get("stemlab_live_upconverter_path")
        self.live_enabled = self.program_variant in ("COHI-Stream", "COHI-Player+Stream")
        self.live_stations_file = self._raw_config.get("live_stations_file")

        self.stemlab_live_lo_freq = self._raw_config.get("stemlab_live_lo_freq")
        self.stemlab_live_sample_rate = self._raw_config.get("stemlab_live_sample_rate")
        stemlab_live_needed = (self.hwconfig == "stemlab" and self.program_variant == "COHI-Stream")
        for name, value in (("stemlab_live_lo_freq", self.stemlab_live_lo_freq),
                             ("stemlab_live_sample_rate", self.stemlab_live_sample_rate)):
            if value is None:
                if stemlab_live_needed:
                    raise ConfigError(tr(
                        "{key} must be set in {file} for hwconfig: stemlab and program_variant: "
                        "COHI-Stream (there is no WAV file there from which LO frequency/sample "
                        "rate could be taken automatically)").format(key=name, file=self.CONFIG_FILE))
                continue
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(tr("invalid value for {key} in {file}: {error}").format(
                    key=name, file=self.CONFIG_FILE, error=exc)) from exc
            if value <= 0:
                raise ConfigError(tr("{key} in {file} must be positive").format(
                    key=name, file=self.CONFIG_FILE))
            if name == "stemlab_live_lo_freq":
                self.stemlab_live_lo_freq = value
            else:
                self.stemlab_live_sample_rate = value

        self.log_mode = normalize_log_mode(self._raw_config.get("log_mode", DEFAULT_LOG_MODE))
        if self.log_mode not in VALID_LOG_MODES:
            raise ConfigError(tr("invalid value for {key} in {file} (known: {valid})").format(
                key="log_mode", file=self.CONFIG_FILE, valid=", ".join(VALID_LOG_MODES)))

        raw_language = self._raw_config.get("language")
        self.language = normalize_language(raw_language)
        if raw_language is not None and str(raw_language).strip().lower() not in ("en", "de", "auto"):
            logger.warning("unknown value for language in %s: %r (known: en, de, auto) - using English",
                           self.CONFIG_FILE, raw_language)

    def save_host_address(self, address):
        self.host_address = address
        self._raw_config["address"] = address
        try:
            with open(self.CONFIG_FILE, "r") as stream:
                text = stream.read()
        except OSError as exc:
            logger.error("%s could not be read to update the address: %s",
                         self.CONFIG_FILE, exc)
            return

        new_text = replace_yaml_value(text, "address", address)
        if new_text is None:
            logger.warning("key 'address:' not found in %s - appending a new line",
                            self.CONFIG_FILE)
            new_text = text.rstrip("\n") + f"\naddress: {address}\n"

        try:
            with open(self.CONFIG_FILE, "w") as stream:
                stream.write(new_text)
        except OSError as exc:
            logger.error("%s could not be written: %s", self.CONFIG_FILE, exc)

    def source_index_for_path(self, file_path):
        target = Path(file_path)
        for index, media_path in enumerate(self.media_paths, start=1):
            if Path(media_path) in target.parents:
                return index
        return None

    def scan_tracks(self):
        self.tracks = []
        header_tools = WavHeaderTools()

        for media_path in self.media_paths:
            start_files = sorted(str(p) for p in Path(media_path).rglob("*_0.wav"))
            logger.info("directory %s: %d WAV start file(s) found", media_path, len(start_files))

            for start_file in start_files:
                track = self._build_track(start_file, header_tools)
                if track:
                    self.tracks.append(track)

        if not self.tracks:
            logger.warning("no playable WAV files found")

    def _build_track(self, start_file, header_tools):
        header = header_tools.read_header(start_file)
        if not header:
            logger.warning("invalid or unknown WAV header: %s", start_file)
            return None

        if not (header["fmt_nChunkSize"] == 16 and header["wFormatTag"] == 1):
            logger.warning(
                "%s: unsupported WAV format (fmt_nChunkSize=%s, wFormatTag=%s) - skipped",
                start_file, header["fmt_nChunkSize"], header["wFormatTag"])
            return None

        directory = Path(start_file).parent
        segments = []
        stop_byte = header["true_datachunksize"]
        segments.append(TrackSegment(start_file, 0, stop_byte))

        while ".wav" in header["nextfilename"]:
            next_name = header["nextfilename"].rstrip("\x00").strip()
            next_file = directory / next_name
            if not next_file.exists():
                logger.warning("chained file not found: %s", next_file)
                break

            header = header_tools.read_header(str(next_file))
            start_byte = stop_byte + 1
            stop_byte = start_byte + header["true_datachunksize"]
            segments.append(TrackSegment(str(next_file), start_byte, stop_byte))

        return segments
