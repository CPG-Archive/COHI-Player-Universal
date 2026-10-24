####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

JUNKSIZE = 2048 * 4

DATABLOCKSIZE = 1024 * 4

TIMESCALER = 156250

LOG_MODE_OFF = "off"
LOG_MODE_SESSION = "session"
LOG_MODE_APPEND = "append"
VALID_LOG_MODES = (LOG_MODE_OFF, LOG_MODE_SESSION, LOG_MODE_APPEND)
DEFAULT_LOG_MODE = LOG_MODE_SESSION
LOG_FILE_NAME = "system_log.log"


def normalize_log_mode(raw_value):
    if raw_value is False:
        return LOG_MODE_OFF
    return raw_value
