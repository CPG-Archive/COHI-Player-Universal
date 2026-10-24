####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

HEADER_SIZE = 216
NEXTFILENAME_FIELD_LEN = 96

SAMPLERATE_BY_INDEX = {
    0: 125_000,
    1: 250_000,
    2: 500_000,
    3: 1_000_000,
    4: 2_000_000,
}


class WavHeaderTools:
    def read_header(self, filename):
        with open(filename, "rb") as f:
            header = {}
            header["riff_chckID"] = f.read(4).decode("ascii", errors="ignore")
            if "RIFF" not in header["riff_chckID"]:
                return False

            header["filesize"] = int.from_bytes(f.read(4), "little")
            header["wave_string"] = f.read(4).decode("ascii", errors="ignore")
            header["fmt_chckID"] = f.read(4).decode("ascii", errors="ignore")
            header["fmt_nChunkSize"] = int.from_bytes(f.read(4), "little")
            header["wFormatTag"] = int.from_bytes(f.read(2), "little")
            header["nChannels"] = int.from_bytes(f.read(2), "little")
            header["nSamplesPerSec"] = int.from_bytes(f.read(4), "little")
            header["nAvgBytesPerSec"] = int.from_bytes(f.read(4), "little")
            header["nBlockAlign"] = int.from_bytes(f.read(2), "little")
            header["nBitsPerSample"] = int.from_bytes(f.read(2), "little")
            header["sdrtype_chckID"] = f.read(4).decode("utf-8", errors="ignore")
            header["sdr_nChunkSize"] = int.from_bytes(f.read(4), "little")

            if "auxi" in header["sdrtype_chckID"]:
                self._read_auxi_extension(f, header)
            elif "rcvr" in header["sdrtype_chckID"]:
                self._read_rcvr_extension(f, header)
            else:
                logger.warning("unknown SDR header format in %s", filename)
                return False

        header["true_datachunksize"] = os.stat(filename).st_size - HEADER_SIZE
        return header

    @staticmethod
    def _read_auxi_extension(f, header):
        starttime = [int.from_bytes(f.read(2), "little") for _ in range(8)]
        stoptime = [int.from_bytes(f.read(2), "little") for _ in range(8)]
        header["starttime"] = starttime
        header["stoptime"] = stoptime
        header["centerfreq"] = int.from_bytes(f.read(4), "little")
        header["ADFrequency"] = int.from_bytes(f.read(4), "little")
        header["IFFrequency"] = int.from_bytes(f.read(4), "little")
        header["Bandwidth"] = int.from_bytes(f.read(4), "little")
        header["IQOffset"] = int.from_bytes(f.read(4), "little")
        f.read(16)

        raw_name = f.read(NEXTFILENAME_FIELD_LEN).decode("utf-8", errors="ignore")
        header["nextfilename"] = raw_name.replace("\\\\", "\\")

        header["data_ckID"] = f.read(4).decode("utf-8", errors="ignore")
        header["data_nChunkSize"] = int.from_bytes(f.read(4), "little")

        header["starttime_dt"] = datetime(starttime[0], starttime[1], starttime[3],
                                           starttime[4], starttime[5], starttime[6])
        try:
            header["stoptime_dt"] = datetime(stoptime[0], stoptime[1], stoptime[3],
                                              stoptime[4], stoptime[5], stoptime[6])
        except ValueError:
            header["stoptime_dt"] = header["starttime_dt"]

    @staticmethod
    def _read_rcvr_extension(f, header):
        header["centerfreq"] = int.from_bytes(f.read(4), "little")
        header["SamplingRateIdx"] = int.from_bytes(f.read(4), "little")
        header["starttime_epoch"] = int.from_bytes(f.read(4), "little")
        header["wAttenId"] = int.from_bytes(f.read(2), "little")
        header["bAdcPresel"] = f.read(1)
        header["bAdcPreamp"] = f.read(1)
        header["bAdcDither"] = f.read(1)
        header["bSpare"] = f.read(1)
        f.read(16)
        header["data_ckID"] = f.read(4).decode("utf-8", errors="ignore")
        header["data_nChunkSize"] = int.from_bytes(f.read(4), "little")

        header["starttime_dt"] = datetime.fromtimestamp(header["starttime_epoch"])
        header["samplingrate"] = SAMPLERATE_BY_INDEX.get(header["SamplingRateIdx"])
        if header["samplingrate"] is None:
            logger.warning("unknown sample rate index in rcvr header: %s",
                            header["SamplingRateIdx"])

        header["nextfilename"] = ""


class RPListener:
    def __init__(self):
        self.devices = []

    def remove_service(self, zeroconf, service_type, name):
        pass

    def add_service(self, zeroconf, service_type, name):
        if not (name.startswith("rp-") and name.endswith(".local.")):
            return

        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return
        address = ".".join(str(b) for b in info.addresses[0])
        self.devices.append((name.strip("."), address))

    def update_service(self, zeroconf, service_type, name):
        pass
