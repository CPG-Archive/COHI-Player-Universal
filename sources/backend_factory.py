####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging

from i18n import tr

logger = logging.getLogger(__name__)

KNOWN_HWCONFIGS = ("stemlab", "fl2k")


def _resolve_fl2k_upconverter(value):
    return value if value in ("native", "cpp") else "ffmpeg"


def create_backend(config):
    logger.info("creating backend for hwconfig=%s ...", config.hwconfig)

    if config.hwconfig == "fl2k":
        resolved_upconverter = _resolve_fl2k_upconverter(config.fl2k_upconverter)
        if resolved_upconverter != config.fl2k_upconverter:
            logger.warning(
                "unknown value fl2k_upconverter='%s' in config_sdr.yaml "
                "(known: ffmpeg, native, cpp) - falling back to 'ffmpeg'",
                config.fl2k_upconverter)

        if resolved_upconverter == "native":
            from fl2k_native_backend import Fl2kNativeBackend
            backend = Fl2kNativeBackend(fl2k_tool_path=config.fl2k_tool_path)
        elif resolved_upconverter == "cpp":
            from fl2k_cpp_backend import Fl2kCppBackend
            backend = Fl2kCppBackend(fl2k_tool_path=config.fl2k_tool_path,
                                      upconverter_tool_path=config.fl2k_cpp_upconverter_path,
                                      live_enabled=config.live_enabled,
                                      live_stations_file=config.live_stations_file)
        else:
            from fl2k_backend import Fl2kBackend
            backend = Fl2kBackend(fl2k_tool_path=config.fl2k_tool_path,
                                   ffmpeg_tool_path=config.ffmpeg_tool_path)
    elif config.hwconfig == "stemlab":
        from stemlab_backend import StemlabBackend
        backend = StemlabBackend(live_enabled=config.live_enabled,
                                  program_variant=config.program_variant,
                                  live_stations_file=config.live_stations_file,
                                  live_upconverter_tool_path=config.stemlab_live_upconverter_path)
    else:
        raise ValueError(tr("unknown hwconfig '{value}' in config_sdr.yaml (known: {known})").format(
            value=config.hwconfig, known=", ".join(KNOWN_HWCONFIGS)))

    logger.info("backend for hwconfig=%s created", config.hwconfig)
    return backend


def backend_display_name(config):
    if config.hwconfig == "fl2k":
        return f"fl2k / {_resolve_fl2k_upconverter(config.fl2k_upconverter)}"
    if config.hwconfig == "stemlab":
        return "STEMlab"
    return config.hwconfig
