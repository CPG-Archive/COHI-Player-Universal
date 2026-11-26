#!/usr/bin/env python3
"""
cohi_player_setup.py  -  Installationsprogramm für COHI-Player-Universal
                         (Linux / Raspberry Pi OS)

Stufe 1 (Konsole, nur Python-Standardbibliothek) bereitet nur Stufe 2 vor:
    - holt bei Bedarf eine neuere Version dieses Installers von GitHub
    - installiert PyQt5 (python3-pyqt5) für das Fenster, falls es fehlt
    - startet Stufe 2

Stufe 2 (Fenster mit PyQt5, ohne Desktop in der Konsole):
    Block "COHI-Player" (Vorgabe: ausgewählt, außer er ist schon aktuell)
        - Update-Prüfung: installierter Stand gegen GitHub, Liste der Änderungen
        - Installationsverzeichnis (Vorgabe ~/COHIRADIA)
        - Python-Pakete systemweit (Raspberry Pi) oder in <Ziel>/venv (PC)
        - Programmdateien laden, Systempakete (apt) und Python-Pakete installieren,
          Inhalt von sources/ nach <Ziel>/COHI-Player kopieren (eigene
          config_sdr.yaml und Senderlisten bleiben erhalten)
        - optional: make clean && make, osmo-fl2k (README 4.3), Desktop-Icons
          "COHI-Player", "Config COHI" und "COHI Update"
    Block "Optionale Erweiterungen"
        - Beispieldateien herunterladen (Vorgabe: nicht ausgewählt), Liste in
          installer/sample_files.txt im Repository; vorhandene Dateien werden
          nach Rückfrage ersetzt und als <Name>.bak-<Datum-Uhrzeit> gesichert
        - AMWaveSynthPropagationSimulator (Vorgabe: nicht ausgewählt) nach
          ~/AMWaveSynthPropagationSimulator mit eigener venv (uv, Python 3.13)
          und Desktop-Icon; nur auf einem PC sinnvoll; kein Update, eine
          vorhandene Installation wird nach einer Warnung überschrieben

Aufruf:
    python3 cohi_player_setup.py                    Erstinstallation / Update
    python3 cohi_player_setup.py --target DIR       anderes Verzeichnis
    python3 cohi_player_setup.py --yes --python-env system --fl2k
                                                    ohne Rückfragen (Konsole)
    python3 cohi_player_setup.py --no-gui           Stufe 2 in der Konsole
    python3 cohi_player_setup.py --no-cohi --amwave nur AMWaveSynthPropagationSimulator
    python3 ~/COHIRADIA/cohi_player_setup.py --update
                                                    Update der vorhandenen Installation
                                                    (so startet auch das Icon "COHI Update")
    python3 cohi_player_setup.py --source-zip FILE  ZIP des Repositories
                                                    statt Download verwenden
    python3 cohi_player_setup.py --lang en          Sprache: auto (Vorgabe, nach der
                                                    Systemsprache: Deutsch oder
                                                    Englisch), de oder en
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Version dieses Installers: beim Ändern erhöhen, sonst holen sich vorhandene
# Installationen die neue Version beim Update nicht (Selbst-Update nur bei
# höherer Versionsnummer)
SETUP_VERSION = "1.5.0"

REPO = "CPG-Archive/COHI-Player-Universal"
BRANCH = "master"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
API_URL = f"https://api.github.com/repos/{REPO}"
PY_MIN = (3, 9)

APP_SUBDIR = "COHI-Player"                 # <Ziel>/COHI-Player = Inhalt von sources/
ICON_REPO_PATH = "installer/cohi-player.svg"
SETUP_REPO_PATH = "installer/cohi_player_setup.py"     # dieser Installer im Repository
SETUP_NAME = "cohi_player_setup.py"                    # Kopie in <Ziel>/ für Updates
REEXEC_ENV = "COHI_SETUP_REEXEC"
EXTRA_FILES = ("README.md", "LICENSE", "requirements.txt")   # landen in <Ziel>/
STATE_FILE = ".cohi_player_setup.json"
LOG_FILE = "cohi_player_setup.log"
# Dateien, die der Benutzer verändert: bei einem Update nicht überschreiben,
# die neue Version wird als <name>.new daneben gelegt
KEEP_FILES = ("config_sdr.yaml", "Web-Streams.csv", "Web-Streams-Locations.csv")
CPP_TOOLS = ("cohi_native_upconverter", "cohi_stemlab_live_upconverter")

FL2K_GIT = "https://gitea.osmocom.org/sdr/osmo-fl2k"
# README 4.3, Schritt 1 (+ pkg-config, das cmake für libusb braucht)
FL2K_BUILD_PKGS = ["libusb-1.0-0-dev", "cmake", "git", "make", "gcc", "pkg-config"]

LAUNCHERS = [
    # Dateiname, Name, Skript, Kommentar (de, en)
    ("cohi-player.desktop", "COHI-Player", "cohi_player_main.py",
     ("Rundfunkbänder und Internetradio für historische Radios",
      "Broadcast band recordings and internet radio for vintage radios")),
    ("cohi-config.desktop", "Config COHI", "cohi_config_gui.py",
     ("Grundeinstellungen des COHI-Players",
      "Basic settings of COHI-Player")),
]
# --- AMWaveSynthPropagationSimulator (eigenständiges Zusatzprogramm) ---------
AMW_NAME = "AMWaveSynthPropagationSimulator"
AMW_REPO = "radiolab81/AMWaveSynthPropagationSimulator"
AMW_BRANCH = "main"
AMW_ZIP_URL = f"https://github.com/{AMW_REPO}/archive/refs/heads/{AMW_BRANCH}.zip"
AMW_MAIN = "AMWaveSynthPropagationSIM.py"
AMW_PYTHON = "3.13"
# Systempakete laut README ("in one go")
AMW_APT = ["build-essential", "cmake", "git", "python3-tk", "python3-gpxpy", "python3-geopy"]
UV_INSTALL_URL = "https://astral.sh/uv/install.sh"
AMW_IMPORT_CHECK = "import tkinter, customtkinter, tkintermapview, geopy, PIL, requests_cache"
AMW_ICON_NAME = "amwavesynth-propagation-sim.svg"
AMW_LAUNCHER = ("amwavesynth-propagation-sim.desktop", "AMWaveSynth Propagation Simulator",
                ("Ausbreitungssimulation für AMWaveSynth und COHI-Player",
                 "Propagation simulation for AMWaveSynth and COHI-Player"))
# eigenes, schlichtes Icon: Sendemast mit Wellen über einer Erdkugel mit Tag-/Nachtgrenze
AMW_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#13314d"/>
  <circle cx="32" cy="48" r="17" fill="#3b82b8"/>
  <path d="M32 31 A17 17 0 0 1 32 65 Z" fill="#0a1a2b" opacity="0.75"/>
  <circle cx="32" cy="48" r="17" fill="none" stroke="#9cc7e6" stroke-width="1.5"/>
  <g fill="none" stroke="#f2c14e" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
    <path d="M25 37 L32 13 L39 37 M27.5 29 H36.5 M29.5 22 H34.5"/>
    <path d="M38 9 A8 8 0 0 1 38 19"/>
    <path d="M42 5 A13 13 0 0 1 42 23"/>
    <path d="M26 9 A8 8 0 0 0 26 19"/>
    <path d="M22 5 A13 13 0 0 0 22 23"/>
  </g>
</svg>
"""

UPDATE_LAUNCHER = ("cohi-update.desktop", "COHI Update",
                   ("COHI-Player aus dem GitHub-Repository aktualisieren",
                    "Update COHI-Player from the GitHub repository"))

# ---------------------------------------------------------------------------
# Sprache: --lang auto|de|en (Vorgabe auto). auto = Deutsch, wenn das System
# deutsch eingestellt ist, sonst Englisch
# ---------------------------------------------------------------------------
LANG_CHOICES = ("auto", "de", "en")


def system_language():
    """'de' oder 'en' nach der Spracheinstellung des Systems (Reihenfolge wie
    bei gettext: LANGUAGE, LC_ALL, LC_MESSAGES, LANG)"""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "").split(":")[0].strip().lower()
        if not val or val in ("c", "posix") or val.startswith("c."):
            continue
        return "de" if val.startswith("de") else "en"
    return "en"


def resolve_language(choice):
    choice = (choice or "auto").lower()
    return choice if choice in ("de", "en") else system_language()


def _lang_from_argv(argv):
    """--lang schon beim Laden auswerten, damit auch --help die Sprache hat"""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return "auto"


LANG_CODE = resolve_language(_lang_from_argv(sys.argv[1:]))
GERMAN = LANG_CODE == "de"


def set_language(choice):
    global LANG_CODE, GERMAN
    LANG_CODE = resolve_language(choice)
    GERMAN = LANG_CODE == "de"


def T(de, en):
    return de if GERMAN else en


# ---------------------------------------------------------------------------
# Allgemeine Hilfsfunktionen
# ---------------------------------------------------------------------------
def is_arm():
    return platform.machine().lower() in ("aarch64", "arm64", "armv7l", "armv6l")


def default_env():
    return "system" if is_arm() else "venv"


def venv_python(target):
    return os.path.join(target, "venv", "bin", "python")


def user_name():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


# Netzwerke, die IPv6 ankündigen, aber nicht routen, sind nicht selten; dann
# wartet jede Verbindung erst auf einen IPv6-Timeout. IPv4 zuerst probieren.
_orig_getaddrinfo = socket.getaddrinfo


def prefer_ipv4():
    socket.getaddrinfo = lambda *a, **k: sorted(_orig_getaddrinfo(*a, **k),
                                                key=lambda ai: ai[0] != socket.AF_INET)


SETUP_LOG = None     # <Ziel>/cohi_player_setup.log


def file_log(msg):
    """an die Protokolldatei anhängen, damit fehlgeschlagene Installationen
    nachträglich untersucht werden können"""
    if not SETUP_LOG:
        return
    try:
        with open(SETUP_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def interactive():
    return bool(sys.stdin) and sys.stdin.isatty()


class ConsoleUI:
    """Ein- und Ausgabe in der Konsole. Die grafische Oberfläche (Schritt 2)
    stellt dieselben Methoden bereit."""
    graphical = False

    def __init__(self, assume_yes=False):
        self.assume_yes = assume_yes

    def log(self, msg):
        print(msg, flush=True)
        file_log(msg)

    def heading(self, msg):
        self.log("\n" + "=" * 72 + "\n  " + msg + "\n" + "=" * 72)

    def progress(self, done, total):
        if total:
            pct = 100 * done // total
            print(f"\r  {done / 1e6:6.1f} MB  {pct:3d} %", end="" if done < total else "\n", flush=True)
        else:
            print(f"\r  {done / 1e6:6.1f} MB", end="", flush=True)

    def ask(self, question, default=True):
        """Ja/Nein-Frage; mit --yes oder ohne Terminal gilt die Vorgabe"""
        if self.assume_yes or not interactive():
            self.log(f"{question} -> {T('ja', 'yes') if default else T('nein', 'no')}")
            return default
        hint = T("[J/n]", "[Y/n]") if default else T("[j/N]", "[y/N]")
        while True:
            ans = input(f"{question} {hint} ").strip().lower()
            if not ans:
                return default
            if ans in ("j", "ja", "y", "yes"):
                return True
            if ans in ("n", "nein", "no"):
                return False

    def choice(self, question, options, default):
        """options: [(key, text)], gibt key zurück"""
        keys = [k for k, _ in options]
        if self.assume_yes or not interactive():
            self.log(f"{question} -> {default}")
            return default
        print(question)
        for i, (k, text) in enumerate(options, start=1):
            mark = T("  (Vorgabe)", "  (default)") if k == default else ""
            print(f"  {i}) {text}{mark}")
        while True:
            ans = input(T("Auswahl", "Choice") + f" [{keys.index(default) + 1}]: ").strip()
            if not ans:
                return default
            if ans.isdigit() and 1 <= int(ans) <= len(keys):
                return keys[int(ans) - 1]

    def text(self, question, default):
        if self.assume_yes or not interactive():
            return default
        ans = input(f"{question} [{default}]: ").strip()
        return ans or default

    def step(self, done, total):
        pass

    def info(self, msg):
        self.log(msg)


def run_logged(cmd, log, cwd=None, heartbeat=None, env=None):
    """cmd ausführen und die Ausgabe protokollieren; mit heartbeat=N Sekunden
    erscheint eine 'bitte warten'-Zeile, wenn das Programm N s lang still ist"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=cwd, text=True, errors="replace", env=env)
    except OSError as exc:
        log(f"  {cmd[0]}: {exc}")
        return 127
    start = time.monotonic()
    last = [start]
    done = threading.Event()

    def ticker():
        while not done.wait(5):
            now = time.monotonic()
            if heartbeat and now - last[0] >= heartbeat:
                last[0] = now
                log(T(f"  ... läuft noch ({int(now - start) // 60} min), bitte warten ...",
                      f"  ... still working ({int(now - start) // 60} min elapsed), please wait ..."))

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            last[0] = time.monotonic()
            log("  " + line.rstrip())
        return proc.wait()
    finally:
        done.set()


# ---------------------------------------------------------------------------
# Administratorrechte und Systempakete (apt)
# ---------------------------------------------------------------------------
def _sudo_without_password():
    if not shutil.which("sudo"):
        return False
    return subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def run_root(ui, commands, why, manual_steps):
    """Shell-Befehle nach Rückfrage als root ausführen. Auf dem Raspberry Pi
    braucht sudo meist kein Passwort; sonst pkexec (Fenster) oder sudo (Terminal)."""
    script = " && ".join(commands)
    if not ui.ask(T(f"{why}\n\nDafür werden Administratorrechte benötigt für:\n\n  {script}\n\n"
                    "Jetzt ausführen?",
                    f"{why}\n\nThe following commands need administrator rights:\n\n  {script}\n\n"
                    "Run them now?")):
        manual_steps.append("sudo sh -c \"" + script + "\"")
        return False
    if os.geteuid() == 0:
        cmd = ["sh", "-c", script]
    elif _sudo_without_password():
        cmd = ["sudo", "-n", "sh", "-c", script]
    elif ui.graphical and shutil.which("pkexec") and (os.environ.get("DISPLAY")
                                                      or os.environ.get("WAYLAND_DISPLAY")):
        cmd = ["pkexec", "sh", "-c", script]
    elif shutil.which("sudo") and interactive():
        ui.log(T("Bitte das Passwort des Benutzers eingeben, falls danach gefragt wird.",
                 "Please enter your password if asked for it."))
        cmd = ["sudo", "sh", "-c", script]
    else:
        ui.log(T("Keine Möglichkeit, Administratorrechte zu bekommen. Bitte von Hand ausführen:",
                 "No way to obtain administrator rights. Please run manually:")
               + "\n  sudo sh -c \"" + script + "\"")
        manual_steps.append("sudo sh -c \"" + script + "\"")
        return False
    ui.log("$ " + " ".join(cmd[:-1]) + " '" + script + "'")
    rc = run_logged(cmd, ui.log, heartbeat=60)
    if rc == 127 and cmd[0] == "pkexec" and shutil.which("sudo") and interactive():
        # pkexec ohne Authentifizierungsagent: Passwort im Terminal abfragen
        ui.log(T("pkexec nicht möglich, bitte das Passwort im Terminal eingeben.",
                 "pkexec not possible, please enter the password in the terminal."))
        rc = run_logged(["sudo", "sh", "-c", script], ui.log, heartbeat=60)
    if rc != 0:
        ui.log(T(f"Befehl fehlgeschlagen (Rückgabewert {rc})", f"command failed (exit code {rc})"))
        manual_steps.append("sudo sh -c \"" + script + "\"")
    return rc == 0


def dpkg_installed(pkg):
    r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg],
                       capture_output=True, text=True)
    return r.returncode == 0 and "install ok installed" in r.stdout


def apt_install_commands(missing):
    """Shell-Befehle (als root), die die Pakete installieren. Erst alle zusammen;
    scheitert das (z.B. ein Paket unbekannt in dieser Distribution), einzeln,
    damit die übrigen trotzdem installiert werden."""
    names = " ".join(missing)
    return ["apt-get update", "export DEBIAN_FRONTEND=noninteractive",
            f"(apt-get install -y {names} || "
            f"(for p in {names}; do apt-get install -y \"$p\" || echo \"WARNING: $p\"; done))"]


def ensure_apt_packages(ui, pkgs, why, manual_steps):
    """fehlende Debian-Pakete mit apt installieren; True, wenn danach alle da sind"""
    if not (shutil.which("dpkg-query") and shutil.which("apt-get")):
        msg = T("Kein apt gefunden. Bitte mit der Paketverwaltung installieren: ",
                "apt not found. Please install with your package manager: ") + " ".join(pkgs)
        ui.log(msg)
        manual_steps.append(msg)
        return False
    missing = [p for p in pkgs if not dpkg_installed(p)]
    if not missing:
        ui.log(T("Systempakete vorhanden: ", "system packages present: ") + ", ".join(pkgs))
        return True
    run_root(ui, apt_install_commands(missing),
             why + "\n" + T("Fehlende Pakete: ", "Missing packages: ") + ", ".join(missing),
             manual_steps)
    still = [p for p in missing if not dpkg_installed(p)]
    if still:
        ui.log(T("Nicht installiert: ", "not installed: ") + ", ".join(still))
    return not still


def base_packages(env):
    """Systempakete für die gewählte Python-Umgebung (README 4.1)"""
    pkgs = ["ffmpeg", "build-essential", "qttranslations5-l10n"]
    if env == "system":
        pkgs += ["python3-pyqt5", "python3-pip"]
    else:
        pkgs += ["python3-venv"]
        if is_arm():
            # pip müsste PyQt5 auf ARM aus den Quellen bauen (Stunden)
            pkgs += ["python3-pyqt5"]
        else:
            # werden von den PyQt5-Wheels (xcb-Plattform) gebraucht
            pkgs += ["libxcb-xinerama0", "libxkbcommon-x11-0"]
    return pkgs


# ---------------------------------------------------------------------------
# Python-Pakete
# ---------------------------------------------------------------------------
def pip_patience():
    return T(
        "\n************************************************************************\n"
        "  Die Python-Pakete werden installiert (numpy, PyYAML, pyqtgraph, ...).\n"
        "  Je nach Internetverbindung kann das einige Minuten dauern.\n"
        "  Bitte Geduld und das Fenster NICHT schließen.\n"
        "************************************************************************\n",
        "\n************************************************************************\n"
        "  Installing the Python packages (numpy, PyYAML, pyqtgraph, ...).\n"
        "  Depending on the internet connection this can take several minutes.\n"
        "  Please be patient and do NOT close this window.\n"
        "************************************************************************\n")

IMPORT_CHECK = "import PyQt5.QtWidgets, yaml, numpy, psutil, paramiko, zeroconf, pyqtgraph"
# Pakete, die der Installer auch dann installiert, wenn sie in einer älteren
# requirements.txt noch fehlen (pyqtgraph war bis V1.2.1 nur optional)
REQUIRED_EXTRA = ("pyqtgraph",)


def create_venv(ui, target):
    venv_dir = os.path.join(target, "venv")
    if os.path.isfile(venv_python(target)):
        ui.log(T("virtuelle Umgebung vorhanden: ", "virtual environment present: ") + venv_dir)
        return True
    cmd = [sys.executable, "-m", "venv"]
    if is_arm():
        cmd.append("--system-site-packages")      # PyQt5 aus python3-pyqt5
    cmd.append(venv_dir)
    ui.log(T("erzeuge virtuelle Umgebung ", "creating virtual environment ") + venv_dir)
    if run_logged(cmd, ui.log) == 0 and os.path.isfile(venv_python(target)):
        return True
    shutil.rmtree(venv_dir, ignore_errors=True)
    ui.log(T("Die virtuelle Umgebung konnte nicht erzeugt werden. Unter Debian/Ubuntu fehlt "
             "meist das Paket python3-venv:\n  sudo apt install python3-venv",
             "Creating the virtual environment failed. On Debian/Ubuntu the package "
             "python3-venv is usually missing:\n  sudo apt install python3-venv"))
    return False


def filtered_requirements(src_text, env, dest):
    """requirements.txt ohne PyQt5, wenn es vom System kommt (system-Modus, ARM)"""
    lines = src_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if env == "system" or is_arm():
        lines = [l for l in lines if not l.strip().lower().startswith("pyqt5")]
    present = {l.strip().split(";")[0].split("=")[0].split("<")[0].split(">")[0].strip().lower()
               for l in lines if l.strip() and not l.lstrip().startswith("#")}
    lines += [name + "\n" for name in REQUIRED_EXTRA if name.lower() not in present]
    with open(dest, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return dest


def pip_install(ui, python, req, env):
    if subprocess.run([python, "-m", "pip", "--version"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        ui.log(T("pip fehlt (Paket python3-pip)", "pip is missing (package python3-pip)"))
        return False
    cmd = [python, "-m", "pip", "install", "-r", req]
    if env == "system":
        # README 4.1: pip install -r requirements.txt --break-system-packages
        # (ältere pip-Versionen, z.B. Raspberry Pi OS Bullseye, kennen die Option nicht)
        helptext = subprocess.run([python, "-m", "pip", "install", "--help"],
                                  capture_output=True, text=True).stdout
        if "--break-system-packages" in helptext:
            cmd.append("--break-system-packages")
        if os.geteuid() != 0:
            cmd.append("--user")
    else:
        run_logged([python, "-m", "pip", "install", "--upgrade", "pip"], ui.log)
    ui.log(pip_patience())
    rc = run_logged(cmd, ui.log, heartbeat=60)
    if rc != 0:
        ui.log(T("pip install ist fehlgeschlagen", "pip install failed"))
    return rc == 0


def python_ready(python):
    """alle Module der requirements.txt importierbar?"""
    return subprocess.run([python, "-c", IMPORT_CHECK], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


# ---------------------------------------------------------------------------
# Download und Kopieren der Programmdateien
# ---------------------------------------------------------------------------
_UA = {"User-Agent": "COHI-Player-setup"}


def http_download(url, dest, progress=None, retries=3):
    tmp = dest + ".part"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            # bricht die Verbindung vorzeitig ab, liefert read() einfach kein
            # weiteres Stück: deshalb die Größe selbst prüfen
            if total and done != total:
                raise OSError(T(f"unvollständig: {done} von {total} Bytes empfangen",
                                f"incomplete: received {done} of {total} bytes"))
            if progress:
                progress(done, done)
            os.replace(tmp, dest)
            return dest
        except urllib.error.HTTPError as exc:
            _remove_quietly(tmp)
            if 400 <= exc.code < 500:          # z.B. 404: Wiederholen hilft nicht
                raise
            last = exc
            time.sleep(3 * (attempt + 1))
        except (OSError, urllib.error.URLError) as exc:
            _remove_quietly(tmp)
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def github_json(path):
    req = urllib.request.Request(API_URL + path,
                                 headers=dict(_UA, Accept="application/vnd.github+json"))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_commit():
    """Commit-ID des aktuellen Stands auf GitHub, '' wenn nicht erreichbar"""
    try:
        return github_json(f"/commits/{BRANCH}").get("sha", "")
    except Exception:
        return ""


def changes_since(old, new, limit=15):
    """(Anzahl, [erste Zeile der Commit-Meldungen, neueste zuerst]) oder (None, [])"""
    try:
        j = github_json(f"/compare/{old}...{new}")
    except Exception:
        return None, []
    msgs = [c.get("commit", {}).get("message", "").splitlines()[0]
            for c in j.get("commits", []) if c.get("commit", {}).get("message")]
    return j.get("total_commits", len(msgs)), list(reversed(msgs))[:limit]


def zip_root(z):
    """Oberster Ordner im GitHub-ZIP, z.B. 'COHI-Player-Universal-master/'"""
    first = z.namelist()[0]
    return first.split("/", 1)[0] + "/"


def read_zip_member(zpath, repo_path):
    with zipfile.ZipFile(zpath) as z:
        return z.read(zip_root(z) + repo_path)


def zip_commit(zpath):
    """GitHub schreibt die Commit-ID als Kommentar in das ZIP"""
    if not zpath:
        return ""
    try:
        with zipfile.ZipFile(zpath) as z:
            c = z.comment.decode("ascii", "replace").strip()
        return c if len(c) == 40 else ""
    except (OSError, zipfile.BadZipFile):
        return ""


def _write_file(dest, data, executable=False):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".cohi_tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    if executable:
        os.chmod(dest, 0o755)


def extract_zip_to(zpath, dest):
    """Ganzes GitHub-ZIP (ohne den obersten Ordner) nach dest entpacken und
    vorhandene Dateien überschreiben; gibt die Anzahl der Dateien zurück"""
    count = 0
    with zipfile.ZipFile(zpath) as z:
        root = zip_root(z)
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            parts = info.filename[len(root):].split("/")
            if not parts or ".." in parts or info.filename.startswith("/"):
                continue
            _write_file(os.path.join(dest, *parts), z.read(info),
                        executable=bool((info.external_attr >> 16) & 0o111))
            count += 1
    return count


def install_files(ui, zpath, target, previous_files=()):
    """Inhalt von sources/ nach <Ziel>/COHI-Player kopieren. Vom Benutzer
    geänderte Dateien (KEEP_FILES) bleiben erhalten. Gelöscht werden nur Dateien,
    die eine frühere Installation selbst kopiert hat (previous_files) und die es
    im Repository nicht mehr gibt; eigene Dateien, Aufnahmen, Protokolle und
    kompilierte Programme bleiben unangetastet.
    Gibt (app_dir, Liste der installierten Dateien) zurück."""
    app_dir = os.path.join(target, APP_SUBDIR)
    copied, kept, new_versions = 0, 0, []
    files = []
    with zipfile.ZipFile(zpath) as z:
        root = zip_root(z)
        src = root + "sources/"
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or not name.startswith(src):
                continue
            parts = name[len(src):].split("/")
            if ".." in parts or "__pycache__" in parts or name.endswith(".pyc"):
                continue
            rel = "/".join(parts)
            dest = os.path.join(app_dir, *parts)
            data = z.read(info)
            files.append(rel)
            if rel in KEEP_FILES and os.path.isfile(dest):
                with open(dest, "rb") as fh:
                    if fh.read() != data:
                        _write_file(dest + ".new", data)
                        new_versions.append(rel)
                kept += 1
                continue
            _write_file(dest, data, executable=bool((info.external_attr >> 16) & 0o111))
            copied += 1
        names = set(z.namelist())
        if root + ICON_REPO_PATH in names:
            _write_file(os.path.join(target, "icons", os.path.basename(ICON_REPO_PATH)),
                        z.read(root + ICON_REPO_PATH))
        for f in EXTRA_FILES:
            if root + f in names:
                _write_file(os.path.join(target, f), z.read(root + f))
    ui.log(T(f"{copied} Dateien nach {app_dir} kopiert", f"{copied} files copied to {app_dir}"))
    if kept:
        ui.log(T(f"{kept} eigene Einstellungs-/Senderdateien beibehalten",
                 f"{kept} own settings / station files kept"))
    for rel in new_versions:
        ui.log(T(f"  {rel}: Ihre Version bleibt in Gebrauch, die neue Version des Repositories "
                 f"liegt als {rel}.new daneben",
                 f"  {rel}: your version is kept, the new version of the repository "
                 f"is saved as {rel}.new"))
    if copied == 0:
        raise RuntimeError(T("Im ZIP wurde kein Ordner sources/ gefunden",
                             "no folder sources/ found in the ZIP"))
    remove_stale_files(ui, app_dir, previous_files, files)
    return app_dir, files


def remove_stale_files(ui, app_dir, previous_files, current_files):
    """Dateien entfernen, die eine frühere Installation kopiert hat und die im
    Repository nicht mehr vorkommen"""
    root = os.path.realpath(app_dir)
    stale = sorted(set(previous_files) - set(current_files) - set(KEEP_FILES))
    removed = 0
    for rel in stale:
        parts = rel.split("/")
        if not rel or ".." in parts or rel.startswith("/"):
            continue
        path = os.path.realpath(os.path.join(app_dir, *parts))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
            ui.log(T("  entfernt (nicht mehr im Repository): ",
                     "  removed (no longer in the repository): ") + rel)
            # leer gewordene Unterordner ebenfalls entfernen
            d = os.path.dirname(path)
            while d != root and d.startswith(root + os.sep) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        except OSError as exc:
            ui.log(f"  {rel}: {exc}")
    return removed


def setup_version_of(data):
    """SETUP_VERSION aus dem Quelltext eines Installers als Tupel, (0,) wenn keine"""
    import re
    m = re.search(rb'^SETUP_VERSION = "([0-9.]+)"', data or b"", re.M)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).decode().split(".") if x.isdigit())


def setup_from_zip(zpath):
    """Installer aus dem Repository-ZIP, nur wenn er neuer ist als dieser"""
    try:
        data = read_zip_member(zpath, SETUP_REPO_PATH)
    except (OSError, KeyError, zipfile.BadZipFile, TypeError):
        return None
    mine = tuple(int(x) for x in SETUP_VERSION.split("."))
    return data if setup_version_of(data) > mine else None


def install_setup_copy(ui, zpath, target):
    """Kopie des Installers nach <Ziel>/ legen: die Version aus dem Repository,
    wenn sie neuer ist, sonst diese, damit sich der Installer bei Updates
    selbst aktualisiert, aber nie auf eine ältere Version zurückfällt"""
    own = os.path.join(target, SETUP_NAME)
    data = setup_from_zip(zpath)
    if data is None:
        if os.path.abspath(__file__) != own:
            shutil.copy2(os.path.abspath(__file__), own)
        return own
    try:
        with open(own, "rb") as fh:
            if fh.read() == data:
                return own
    except OSError:
        pass
    existed = os.path.isfile(own)
    _write_file(own, data, executable=True)
    if existed:
        ui.log(T("Installer aktualisiert: ", "installer updated: ") + own)
    return own


# ---------------------------------------------------------------------------
# Zustand der Installation
# ---------------------------------------------------------------------------
def load_state(target):
    try:
        with open(os.path.join(target, STATE_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(target, state):
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, STATE_FILE), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Optionale Schritte der Stufe 2
# ---------------------------------------------------------------------------
class Installer:
    def __init__(self, ui, target, python, manual_steps=None, setup_python=None):
        self.ui = ui
        self.target = target
        self.app_dir = os.path.join(target, APP_SUBDIR)
        self.python = python
        # das Python, mit dem Stufe 1 lief (System-Python, auch bei venv)
        self.setup_python = setup_python or shutil.which("python3") or sys.executable
        self.manual_steps = manual_steps if manual_steps is not None else []

    # --- make (README 4.2) -------------------------------------------------
    def run_make(self):
        ui = self.ui
        ui.heading(T("C++-Tools übersetzen (make clean && make)",
                     "Compiling the C++ tools (make clean && make)"))
        if not (shutil.which("g++") and shutil.which("make")):
            ensure_apt_packages(ui, ["build-essential"],
                                T("Zum Uebersetzen werden g++ und make benötigt.",
                                  "g++ and make are needed for compiling."), self.manual_steps)
        if not (shutil.which("g++") and shutil.which("make")):
            self.manual_steps.append(f"cd {self.app_dir} && make clean && make")
            return False
        if run_logged(["make", "clean"], ui.log, cwd=self.app_dir) != 0 \
                or run_logged(["make"], ui.log, cwd=self.app_dir, heartbeat=30) != 0:
            ui.log(T("make ist fehlgeschlagen", "make failed"))
            self.manual_steps.append(f"cd {self.app_dir} && make clean && make")
            return False
        missing = [t for t in CPP_TOOLS if not os.path.isfile(os.path.join(self.app_dir, t))]
        if missing:
            ui.log(T("nach make fehlen: ", "missing after make: ") + ", ".join(missing))
            return False
        ui.log(T("C++-Tools für diesen Rechner übersetzt", "C++ tools compiled for this computer"))
        return True

    # --- osmo-fl2k (README 4.3) ---------------------------------------------
    def install_fl2k(self):
        ui = self.ui
        ui.heading(T("Treiber osmo-fl2k für den FL2K-USB-VGA-Dongle",
                     "Driver osmo-fl2k for the FL2K USB-VGA dongle"))
        # Schritt 1: Werkzeuge und libusb-Header
        if not ensure_apt_packages(ui, FL2K_BUILD_PKGS,
                                   T("Zum Bau von osmo-fl2k werden Werkzeuge und libusb benötigt.",
                                     "Building osmo-fl2k needs build tools and libusb."),
                                   self.manual_steps):
            ui.log(T("osmo-fl2k wird nicht gebaut (Pakete fehlen)",
                     "osmo-fl2k is not built (packages missing)"))
            return False
        # Schritt 2: Quellen holen und als normaler Benutzer bauen
        tools = os.path.join(self.target, "osmotools")
        src = os.path.join(tools, "osmo-fl2k")
        build = os.path.join(src, "build")
        os.makedirs(tools, exist_ok=True)
        if os.path.isdir(os.path.join(src, ".git")):
            ui.log(T("aktualisiere ", "updating ") + src)
            rc = run_logged(["git", "-C", src, "pull", "--ff-only"], ui.log)
        else:
            shutil.rmtree(src, ignore_errors=True)
            rc = run_logged(["git", "clone", FL2K_GIT, src], ui.log, heartbeat=30)
        if rc != 0:
            ui.log(T("git ist fehlgeschlagen (Internetverbindung?)",
                     "git failed (internet connection?)"))
            self.manual_steps.append(T("osmo-fl2k nach README 4.3 installieren",
                                       "install osmo-fl2k as described in README 4.3"))
            return False
        os.makedirs(build, exist_ok=True)
        jobs = str(os.cpu_count() or 1)
        if run_logged(["cmake", "../", "-DINSTALL_UDEV_RULES=ON"], ui.log, cwd=build) != 0 \
                or run_logged(["make", "-j" + jobs], ui.log, cwd=build, heartbeat=30) != 0:
            ui.log(T("Bau von osmo-fl2k fehlgeschlagen", "building osmo-fl2k failed"))
            self.manual_steps.append(f"cd {build} && cmake ../ -DINSTALL_UDEV_RULES=ON && make")
            return False
        # Schritt 2b + 3: installieren, Bibliotheken registrieren, udev-Regeln laden
        cmds = [f"cd '{build}'", "make install", "ldconfig",
                "udevadm control -R", "udevadm trigger"]
        # die udev-Regel von osmo-fl2k gibt der Gruppe plugdev Zugriff
        if not self._in_group("plugdev"):
            cmds.append("(getent group plugdev >/dev/null || groupadd plugdev)")
            cmds.append(f"usermod -aG plugdev {user_name()}")
            self.manual_steps.append(T("einmal ab- und wieder anmelden (oder neu starten), damit "
                                       "der Zugriff auf den FL2K-Dongle ohne root funktioniert",
                                       "log out and in again (or reboot) once, so that the FL2K "
                                       "dongle can be used without root"))
        if not run_root(ui, cmds, T("osmo-fl2k ist gebaut und wird jetzt installiert.",
                                    "osmo-fl2k is built and will be installed now."),
                        self.manual_steps):
            return False
        found = shutil.which("fl2k_file") or (
            "/usr/local/bin/fl2k_file" if os.path.isfile("/usr/local/bin/fl2k_file") else None)
        ui.log(T("osmo-fl2k installiert: ", "osmo-fl2k installed: ") + str(found))
        return bool(found)

    @staticmethod
    def _in_group(group):
        import grp
        try:
            g = grp.getgrnam(group)
        except KeyError:
            return False
        return g.gr_gid in os.getgroups() or user_name() in g.gr_mem

    # --- Desktop-Icons ------------------------------------------------------
    @staticmethod
    def _desktop_dir():
        """Der Desktop-Ordner ist lokalisiert (Desktop, Schreibtisch, ...)"""
        home = os.path.expanduser("~")
        desk = ""
        try:
            desk = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        if desk and os.path.normpath(desk) != os.path.normpath(home):
            return desk
        for name in ("Desktop", "Schreibtisch"):
            if os.path.isdir(os.path.join(home, name)):
                return os.path.join(home, name)
        return os.path.join(home, "Desktop")

    @staticmethod
    def _exec_quote(path):
        """Pfad für die Exec-Zeile einer .desktop-Datei"""
        for ch in ("\\", '"', "`", "$"):
            path = path.replace(ch, "\\" + ch)
        return f'"{path}"'

    def create_launchers(self):
        ui = self.ui
        ui.heading(T("Desktop-Icons und Startmenü", "Desktop icons and menu entries"))
        icon = os.path.join(self.target, "icons", os.path.basename(ICON_REPO_PATH))
        # (Dateiname, Name, Exec, Path, Terminal, Kommentar)
        # Path= ist wichtig: beide Programme lesen config_sdr.yaml aus dem
        # aktuellen Arbeitsverzeichnis
        launchers = [(fname, name,
                      f"{self._exec_quote(self.python)} "
                      f"{self._exec_quote(os.path.join(self.app_dir, script))}",
                      self.app_dir, False, comment)
                     for fname, name, script, comment in LAUNCHERS]
        setup_copy = os.path.join(self.target, SETUP_NAME)
        if os.path.isfile(setup_copy):
            # das Update braucht ein Terminal: Stufe 1 fragt dort ggf. nach dem Passwort
            fname, name, comment = UPDATE_LAUNCHER
            launchers.append((fname, name,
                              f"{self._exec_quote(self.setup_python)} "
                              f"{self._exec_quote(setup_copy)} --update",
                              self.target, True, comment))
        for fname, name, exec_line, path, terminal, comment in launchers:
            write_launcher(ui, fname, name, exec_line, path, icon, terminal, comment)
        return True

    @staticmethod
    def _trust(path):
        """Desktops starten eine .desktop-Datei nur, wenn sie ausführbar und als
        vertrauenswürdig markiert ist: GNOME/Nautilus über metadata::trusted,
        Xfce >= 4.18 über metadata::xfce-exe-checksum"""
        if not shutil.which("gio"):
            return
        with open(path, "rb") as fh:
            checksum = hashlib.sha256(fh.read()).hexdigest()
        for key, value in (("metadata::trusted", "true"),
                           ("metadata::xfce-exe-checksum", checksum)):
            try:
                subprocess.run(["gio", "set", path, key, value], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass


def write_launcher(ui, fname, name, exec_line, path, icon, terminal, comment):
    """Desktop-Eintrag ins Startmenü und auf den Desktop schreiben"""
    menu_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(menu_dir, exist_ok=True)
    desk = Installer._desktop_dir()
    os.makedirs(desk, exist_ok=True)
    c_de, c_en = comment
    entry = ("[Desktop Entry]\n"
             "Version=1.0\n"
             "Type=Application\n"
             f"Name={name}\n"
             f"Comment={T(c_de, c_en)}\n"
             f"Exec={exec_line}\n"
             f"Path={path}\n"
             f"Icon={icon}\n"
             f"Terminal={'true' if terminal else 'false'}\n"
             "Categories=AudioVideo;Audio;HamRadio;\n")
    for folder in (menu_dir, desk):
        dest = os.path.join(folder, fname)
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(entry)
            os.chmod(dest, 0o755)
        except OSError as exc:
            ui.log(f"  {dest}: {exc}")
            continue
        if folder == desk:
            Installer._trust(dest)
        ui.log(f"  {name}: {dest}")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", menu_dir],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# AMWaveSynthPropagationSimulator
# ---------------------------------------------------------------------------
def amwave_dir():
    return os.path.expanduser("~/" + AMW_NAME)


def amwave_installed():
    return os.path.isfile(os.path.join(amwave_dir(), AMW_MAIN))


def amwave_python():
    return os.path.join(amwave_dir(), ".venv", "bin", "python")


def amwave_warnings():
    """Warntexte, die beim Auswählen der Option gezeigt werden"""
    out = []
    if is_arm():
        out.append(T(f"{AMW_NAME} braucht einen großen Bildschirm und ist nur auf einem PC "
                     "sinnvoll, nicht auf dem Raspberry Pi.",
                     f"{AMW_NAME} needs a large screen and only makes sense on a PC, "
                     "not on the Raspberry Pi."))
    d = amwave_dir()
    if os.path.isdir(d) and os.listdir(d):
        out.append(T(f"In {d} ist bereits eine Installation vorhanden. Sie wird überschrieben: "
                     "die Dateien aus dem Repository und die virtuelle Umgebung .venv werden "
                     "ersetzt. Eigene zusätzliche Dateien (z.B. eigene Szenarien in tx_sites, "
                     "app_config.json) bleiben erhalten.",
                     f"An installation already exists in {d}. It will be overwritten: the "
                     "files of the repository and the virtual environment .venv are replaced. "
                     "Own additional files (e.g. own scenarios in tx_sites, app_config.json) "
                     "are kept."))
    return out


def find_uv():
    for cand in (shutil.which("uv"), "/usr/local/bin/uv", os.path.expanduser("~/.local/bin/uv"),
                 os.path.expanduser("~/.cargo/bin/uv")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def install_amwave(ui, manual_steps, skip_apt=False):
    """Installation nach dem README des Repositories:
    apt-Pakete, uv nach /usr/local/bin, Quellen, uv venv --python 3.13, Pakete,
    dazu ein Desktop-Icon"""
    import tempfile
    d = amwave_dir()
    ui.heading(T(f"{AMW_NAME} nach {d}", f"{AMW_NAME} to {d}"))
    for w in amwave_warnings():
        ui.log(T("Hinweis: ", "Note: ") + w)

    # 1. Systempakete und uv (als root, eine gemeinsame Rückfrage)
    cmds, why = [], []
    if skip_apt:
        ui.log(T("Systempakete übersprungen (--no-system-packages)",
                 "system packages skipped (--no-system-packages)"))
    elif shutil.which("dpkg-query") and shutil.which("apt-get"):
        missing = [p for p in AMW_APT if not dpkg_installed(p)]
        if missing:
            cmds += apt_install_commands(missing)
            why.append(T("Fehlende Pakete: ", "Missing packages: ") + ", ".join(missing))
        else:
            ui.log(T("Systempakete vorhanden: ", "system packages present: ") + ", ".join(AMW_APT))
    else:
        manual_steps.append(T("Bitte mit der Paketverwaltung installieren: ",
                              "Please install with your package manager: ") + " ".join(AMW_APT))
    tmpdir = tempfile.mkdtemp(prefix="cohi_setup_")
    try:
        if not find_uv():
            script = os.path.join(tmpdir, "uv-install.sh")
            ui.log(T("lade das Installationsskript von uv: ", "downloading the uv installer: ")
                   + UV_INSTALL_URL)
            try:
                http_download(UV_INSTALL_URL, script)
                os.chmod(script, 0o644)
                os.chmod(tmpdir, 0o755)
                cmds.append(f"env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 "
                            f"sh '{script}'")
                why.append(T("Der Paketmanager uv wird nach /usr/local/bin installiert.",
                             "The package manager uv is installed to /usr/local/bin."))
            except Exception as exc:
                ui.log(T(f"Download fehlgeschlagen: {exc}", f"download failed: {exc}"))
        if cmds:
            run_root(ui, cmds, T(f"{AMW_NAME} braucht einige Systempakete und uv.",
                                 f"{AMW_NAME} needs some system packages and uv.")
                     + "\n" + "\n".join(why), manual_steps)
        uv = find_uv()
        if not uv:
            ui.log(T("uv ist nicht installiert, Abbruch.", "uv is not installed, aborting."))
            manual_steps.append(T(f"uv installieren (README von {AMW_NAME})",
                                  f"install uv (README of {AMW_NAME})"))
            return False
        ui.log("uv: " + uv)

        # 2. Programmdateien (überschreibt eine vorhandene Installation)
        zpath = os.path.join(tmpdir, f"{AMW_NAME}-{AMW_BRANCH}.zip")
        ui.log(AMW_ZIP_URL)
        try:
            http_download(AMW_ZIP_URL, zpath, ui.progress)
        except Exception as exc:
            ui.log(T(f"Download fehlgeschlagen ({exc}). Bitte die Internetverbindung prüfen.",
                     f"Download failed ({exc}). Please check the internet connection."))
            return False
        os.makedirs(d, exist_ok=True)
        n = extract_zip_to(zpath, d)
        ui.log(T(f"{n} Dateien nach {d} kopiert", f"{n} files copied to {d}"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 3. virtuelle Umgebung mit Python 3.13 und Pakete
    venv = os.path.join(d, ".venv")
    if os.path.isdir(venv):
        ui.log(T("ersetze die vorhandene virtuelle Umgebung ", "replacing the existing virtual "
                 "environment ") + venv)
        shutil.rmtree(venv, ignore_errors=True)
    ui.log(T(f"erzeuge die virtuelle Umgebung mit Python {AMW_PYTHON} (uv lädt Python "
             "bei Bedarf selbst herunter)",
             f"creating the virtual environment with Python {AMW_PYTHON} (uv downloads Python "
             "itself if necessary)"))

    def make_venv(preference=None):
        shutil.rmtree(venv, ignore_errors=True)
        cmd = [uv, "venv", "--python", AMW_PYTHON]
        if preference:
            cmd += ["--python-preference", preference]
        return (run_logged(cmd + [".venv"], ui.log, cwd=d, heartbeat=30) == 0
                and os.path.isfile(amwave_python()))

    def has_tkinter():
        return subprocess.run([amwave_python(), "-c", "import tkinter"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    if not make_venv():
        ui.log(T("uv venv ist fehlgeschlagen", "uv venv failed"))
        manual_steps.append(f"cd {d} && uv venv --python {AMW_PYTHON}")
        return False
    if not has_tkinter():
        # uv hat ein vorhandenes Python 3.13 ohne tkinter genommen: dann ein
        # eigenes Python von uv verwenden (das bringt tkinter mit)
        ui.log(T("Das gefundene Python 3.13 hat kein tkinter. uv lädt ein eigenes Python 3.13.",
                 "The Python 3.13 found has no tkinter. uv downloads its own Python 3.13."))
        if not make_venv("only-managed"):
            ui.log(T("Das hat nicht geklappt; es bleibt beim vorhandenen Python. Für die "
                     "Oberfläche wird tkinter gebraucht (Paket python3-tk).",
                     "That did not work; the existing Python is used. The user interface "
                     "needs tkinter (package python3-tk)."))
            if not make_venv():
                return False
    ui.log(T("\n************************************************************************\n"
             f"  Die Python-Pakete für {AMW_NAME} werden installiert.\n"
             "  Je nach Internetverbindung kann das einige Minuten dauern.\n"
             "  Bitte Geduld und das Fenster NICHT schließen.\n"
             "************************************************************************\n",
             "\n************************************************************************\n"
             f"  Installing the Python packages for {AMW_NAME}.\n"
             "  Depending on the internet connection this can take several minutes.\n"
             "  Please be patient and do NOT close this window.\n"
             "************************************************************************\n"))
    if run_logged([uv, "pip", "install", "--python", amwave_python(), "-r", "requirements.txt"],
                  ui.log, cwd=d, heartbeat=60) != 0:
        ui.log(T("Installation der Python-Pakete fehlgeschlagen",
                 "installing the Python packages failed"))
        manual_steps.append(f"cd {d} && uv pip install --python .venv/bin/python "
                            "-r requirements.txt")
        return False
    ok = subprocess.run([amwave_python(), "-c", AMW_IMPORT_CHECK],
                        capture_output=True, text=True)
    if ok.returncode != 0:
        ui.log(T("WARNUNG: nicht alle Module lassen sich laden: ",
                 "WARNING: not all modules can be imported: ")
               + (ok.stderr.strip().splitlines() or ["?"])[-1])

    # 4. Desktop-Icon
    ui.heading(T("Desktop-Icon", "Desktop icon"))
    icon = os.path.join(d, AMW_ICON_NAME)
    _write_file(icon, AMW_ICON_SVG.encode("utf-8"))
    fname, name, comment = AMW_LAUNCHER
    write_launcher(ui, fname, name,
                   f"{Installer._exec_quote(amwave_python())} "
                   f"{Installer._exec_quote(os.path.join(d, AMW_MAIN))}",
                   d, icon, False, comment)
    ui.log(T(f"{AMW_NAME} ist installiert. Start: Desktop-Icon '{name}' oder\n"
             f"  cd {d} && .venv/bin/python {AMW_MAIN}",
             f"{AMW_NAME} is installed. Start: desktop icon '{name}' or\n"
             f"  cd {d} && .venv/bin/python {AMW_MAIN}"))
    return ok.returncode == 0


# ---------------------------------------------------------------------------
# Beispieldateien (installer/sample_files.txt im Repository)
# ---------------------------------------------------------------------------
SAMPLES_REPO_PATH = "installer/sample_files.txt"
SAMPLES_RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SAMPLES_REPO_PATH}"
SAMPLE_SECTIONS = ("COHIRADIA", AMW_NAME)


def fetch_sample_list(source_zip=None):
    """Inhalt von installer/sample_files.txt: aus dem angegebenen ZIP, sonst von GitHub"""
    if source_zip:
        try:
            return read_zip_member(source_zip, SAMPLES_REPO_PATH).decode("utf-8", "replace")
        except (OSError, KeyError, zipfile.BadZipFile):
            pass
    req = urllib.request.Request(SAMPLES_RAW_URL, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_sample_list(text, bases):
    """Liste der Beispieldateien auswerten. Syntax:

        # Kommentar
        COHIRADIA: /WAV                          Zielverzeichnis relativ zum
                                                 Installationsverzeichnis des COHI-Players
        https://.../AM1_Sample_0.wav             Dateien für dieses Verzeichnis
        AMWaveSynthPropagationSimulator: /       relativ zu ~/AMWaveSynthPropagationSimulator

    Eine Zielangabe gilt bis zur nächsten; es darf beliebig viele geben, auch
    mehrere für dasselbe Programm, und Unterverzeichnisse über mehrere Ebenen
    (z.B. COHIRADIA: /WAV/AM/1960), die beim Download angelegt werden.
    bases: {Abschnitt: Basisverzeichnis oder None, wenn das Programm weder
    installiert ist noch jetzt installiert wird}.  Ergebnis: (Einträge, Hinweise)"""
    entries, notes = [], []
    section, dest_dir = None, None
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("http://", "https://")):
            if section is None:
                notes.append(T(f"Zeile {no}: Datei ohne vorherige Zielangabe, übersprungen: {line}",
                               f"line {no}: file without a preceding target, skipped: {line}"))
                continue
            if dest_dir is None:
                continue                          # Abschnitt wird übersprungen (Hinweis steht schon da)
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(line).path))
            if not name or name in (".", ".."):
                notes.append(T(f"Zeile {no}: kein Dateiname in {line}",
                               f"line {no}: no file name in {line}"))
                continue
            entries.append({"url": line, "dir": dest_dir, "dest": os.path.join(dest_dir, name),
                            "name": name, "section": section, "size": None, "error": None})
            continue
        key, colon, rel = line.partition(":")
        if not colon:
            notes.append(T(f"Zeile {no}: nicht verstanden: {line}",
                           f"line {no}: not understood: {line}"))
            continue
        key = key.strip()
        section = next((x for x in SAMPLE_SECTIONS if x.lower() == key.lower()), key)
        dest_dir = None
        if section not in SAMPLE_SECTIONS:
            notes.append(T(f"Zeile {no}: unbekannte Zielangabe '{key}', die folgenden Dateien "
                           "werden übersprungen",
                           f"line {no}: unknown target '{key}', the following files are skipped"))
            continue
        base = bases.get(section)
        if not base:
            notes.append(T(f"{section} ist nicht installiert und wird jetzt nicht installiert; "
                           f"die Dateien für '{rel.strip()}' werden übersprungen.",
                           f"{section} is not installed and is not being installed now; "
                           f"the files for '{rel.strip()}' are skipped."))
            continue
        parts = [x for x in rel.strip().replace("\\", "/").split("/") if x and x != "."]
        if ".." in parts:
            notes.append(T(f"Zeile {no}: '..' ist im Zielverzeichnis nicht erlaubt: {line}",
                           f"line {no}: '..' is not allowed in the target directory: {line}"))
            continue
        dest_dir = os.path.join(base, *parts)
    # jede Zieldatei nur einmal
    seen, unique = set(), []
    for e in entries:
        if e["dest"] in seen:
            notes.append(T(f"doppelt, nur einmal geladen: {e['url']}",
                           f"duplicate, downloaded only once: {e['url']}"))
            continue
        seen.add(e["dest"])
        unique.append(e)
    return unique, notes


def probe_samples(entries):
    """Verfügbarkeit und Größe jeder Datei auf dem Server prüfen (HEAD)"""
    for e in entries:
        try:
            req = urllib.request.Request(e["url"], headers=_UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as resp:
                n = resp.headers.get("Content-Length")
                e["size"] = int(n) if n and n.isdigit() else None
        except urllib.error.HTTPError as exc:
            if exc.code in (405, 501):            # HEAD nicht unterstützt: beim Download sehen
                continue
            e["error"] = f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:
            e["error"] = str(getattr(exc, "reason", exc))


def sample_bases(cfg):
    """Basisverzeichnisse der Abschnitte: nur, wenn das Programm installiert ist
    oder in diesem Lauf installiert wird"""
    target = cfg.get("target") or default_target()
    cohi = target if (cfg.get("cohi") or load_state(target).get("installed")) else None
    amw = amwave_dir() if (cfg.get("amwave") or amwave_installed()) else None
    return {"COHIRADIA": cohi, AMW_NAME: amw}


def plan_samples(cfg):
    """Liste laden, auswerten und auf dem Server prüfen; Fehler beim Laden der
    Liste werden als Ausnahme weitergegeben"""
    text = fetch_sample_list(cfg.get("source_zip"))
    entries, notes = parse_sample_list(text, sample_bases(cfg))
    probe_samples(entries)
    return entries, notes


def fmt_size(n):
    if n is None:
        return "?"
    for unit, f in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= f:
            return f"{n / f:.1f} {unit}"
    return f"{n} B"


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def samples_overview(entries, notes, limit=20):
    """Text für die Rückfrage vor dem Download: (Text, ausführliche Liste oder '')"""
    ok = [e for e in entries if not e["error"]]
    bad = [e for e in entries if e["error"]]
    lines = [f"  {short_path(e['dest'])}   ({fmt_size(e['size'])})" for e in ok]
    out = []
    if ok:
        out.append(T("Folgende Beispieldateien werden heruntergeladen:",
                     "The following sample files will be downloaded:"))
        out += lines[:limit]
        if len(lines) > limit:
            out.append(T(f"  ... und {len(lines) - limit} weitere (siehe Details)",
                         f"  ... and {len(lines) - limit} more (see details)"))
        known = [e["size"] for e in ok if e["size"] is not None]
        if known:
            out.append(T(f"Zusammen: {fmt_size(sum(known))}", f"Total: {fmt_size(sum(known))}")
                       + ("" if len(known) == len(ok) else
                          T(" (ohne Dateien unbekannter Größe)", " (without files of unknown size)")))
            # freier Platz im ersten vorhandenen übergeordneten Verzeichnis
            d = ok[0]["dir"]
            while d and not os.path.isdir(d):
                d = os.path.dirname(d)
            try:
                free = shutil.disk_usage(d or os.path.expanduser("~")).free
                if sum(known) > free * 0.95:
                    out.append(T(f"ACHTUNG: Es sind nur noch {fmt_size(free)} frei!",
                                 f"WARNING: only {fmt_size(free)} free!"))
            except OSError:
                pass
        out.append("")
        out.append(T("Hinweis: Bei großen Dateien kann der Download sehr lange dauern.",
                     "Note: with large files the download can take a very long time."))
    else:
        out.append(T("Es gibt keine Beispieldateien, die heruntergeladen werden können.",
                     "There are no sample files that can be downloaded."))
    if bad:
        out.append("")
        out.append(T("FEHLER: Diese Dateien sind nicht verfügbar und werden übersprungen:",
                     "ERROR: these files are not available and are skipped:"))
        out += [f"  {e['url']}  –  {e['error']}" for e in bad]
    if notes:
        out.append("")
        out.append(T("Hinweise:", "Notes:"))
        out += ["  " + n for n in notes]
    return "\n".join(out), ("\n".join(lines) if len(lines) > limit else "")


def existing_samples(entries):
    return [e for e in entries if not e["error"] and os.path.exists(e["dest"])]


def existing_overview(existing):
    names = [f"  {short_path(e['dest'])}" for e in existing]
    if len(names) > 20:
        names = names[:20] + [T(f"  ... und {len(existing) - 20} weitere",
                                f"  ... and {len(existing) - 20} more")]
    return (T("Diese Beispieldateien gibt es bereits:", "These sample files already exist:")
            + "\n" + "\n".join(names) + "\n\n"
            + T("Sollen sie überschrieben werden? Die vorhandenen Dateien bleiben als Sicherung "
                "<Name>.bak-<Datum-Uhrzeit> erhalten.\n"
                "Nein = nur die fehlenden Dateien herunterladen.",
                "Shall they be overwritten? The existing files are kept as a backup "
                "<name>.bak-<date-time>.\n"
                "No = only download the missing files."))


def download_samples(ui, entries, overwrite):
    """Beispieldateien laden. Eine vorhandene Datei wird erst ersetzt, wenn die
    neue vollständig geladen ist; die alte bleibt als <Name>.bak-<Datum-Uhrzeit>."""
    ui.heading(T("Beispieldateien", "Sample files"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    errors, loaded, skipped = [], 0, 0
    for e in entries:
        if e["error"]:
            errors.append(f"{e['url']}  –  {e['error']}")
            continue
        if os.path.exists(e["dest"]) and not overwrite:
            ui.log(T("  vorhanden, nicht überschrieben: ", "  exists, not overwritten: ")
                   + short_path(e["dest"]))
            skipped += 1
            continue
        os.makedirs(e["dir"], exist_ok=True)
        ui.log(T("lade ", "downloading ") + e["url"]
               + (f"  ({fmt_size(e['size'])})" if e["size"] else ""))
        tmp = e["dest"] + ".download"
        last = [time.monotonic()]

        def progress(done, total, name=e["name"]):
            now = time.monotonic()
            if now - last[0] >= 5:
                last[0] = now
                ui.log(f"  {name}: {fmt_size(done)}"
                       + (f" / {fmt_size(total)} ({100 * done // total} %)" if total else ""))
        try:
            http_download(e["url"], tmp, progress)
        except Exception as exc:
            reason = (f"HTTP {exc.code} {exc.reason}" if isinstance(exc, urllib.error.HTTPError)
                      else str(getattr(exc, "reason", exc)))
            ui.log(T("  FEHLER: ", "  ERROR: ") + reason)
            errors.append(f"{e['url']}  –  {reason}")
            _remove_quietly(tmp)
            continue
        if os.path.exists(e["dest"]):
            backup = f"{e['dest']}.bak-{stamp}"
            os.replace(e["dest"], backup)
            ui.log(T("  Sicherung der alten Datei: ", "  backup of the old file: ")
                   + short_path(backup))
        os.replace(tmp, e["dest"])
        loaded += 1
        ui.log("  -> " + short_path(e["dest"]))
    ui.log(T(f"{loaded} Beispieldatei(en) geladen, {skipped} vorhandene behalten, "
             f"{len(errors)} Fehler",
             f"{loaded} sample file(s) downloaded, {skipped} existing kept, "
             f"{len(errors)} error(s)"))
    if errors:
        ui.info(T("Folgende Beispieldateien konnten nicht heruntergeladen werden:\n\n",
                  "The following sample files could not be downloaded:\n\n")
                + "\n".join("- " + x for x in errors))
    return not errors


# ---------------------------------------------------------------------------
# Gemeinsame Hilfen für Stufe 1 und 2
# ---------------------------------------------------------------------------
SETUP_RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SETUP_REPO_PATH}"


def default_target():
    return os.path.expanduser("~/COHIRADIA")


def resolve_target(args):
    """Installationsverzeichnis: --target, sonst das Verzeichnis, in dem diese
    Kopie des Installers liegt (Update), sonst ~/COHIRADIA"""
    if args.target:
        return os.path.abspath(os.path.expanduser(args.target))
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here, STATE_FILE)):
        return here
    return default_target()


def set_log_for(target):
    """Protokoll ins Installationsverzeichnis, solange es das noch nicht gibt
    ins Cache-Verzeichnis des Benutzers"""
    global SETUP_LOG
    if target and os.path.isdir(target):
        SETUP_LOG = os.path.join(target, LOG_FILE)
    else:
        cache = os.path.expanduser("~/.cache")
        os.makedirs(cache, exist_ok=True)
        SETUP_LOG = os.path.join(cache, LOG_FILE)


def preflight(ui):
    if platform.system() != "Linux":
        ui.log(T("Dieser Installer ist für Linux / Raspberry Pi OS. Unter Windows bitte "
                 "nach Abschnitt 4.4 des README installieren.",
                 "This installer is for Linux / Raspberry Pi OS. On Windows please follow "
                 "section 4.4 of the README."))
        return False
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        ui.log(T("Bitte den Installer OHNE sudo starten:\n  python3 cohi_player_setup.py\n"
                 "Er fragt selbst nach Administratorrechten, wenn sie gebraucht werden.",
                 "Please start the installer WITHOUT sudo:\n  python3 cohi_player_setup.py\n"
                 "It asks for administrator rights itself when they are needed."))
        return False
    if sys.version_info[:2] < PY_MIN:
        ui.log(T(f"COHI-Player braucht Python {PY_MIN[0]}.{PY_MIN[1]} oder neuer, "
                 f"dies ist {platform.python_version()}.",
                 f"COHI-Player needs Python {PY_MIN[0]}.{PY_MIN[1]} or newer, "
                 f"this is {platform.python_version()}."))
        return False
    return True


def display_available():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def pyqt5_available(python):
    return subprocess.run([python, "-c", "import PyQt5.QtWidgets"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


# ---------------------------------------------------------------------------
# Stufe 1: nur den Installer und das Fenster für Stufe 2 bereitstellen
# ---------------------------------------------------------------------------
def self_update(ui, target, args):
    """Neuere Version dieses Installers von GitHub holen und mit ihr neu starten"""
    if os.environ.get(REEXEC_ENV) or args.no_version_check or args.source_zip:
        return
    try:
        req = urllib.request.Request(SETUP_RAW_URL, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
    except Exception:
        return
    mine = tuple(int(x) for x in SETUP_VERSION.split("."))
    if setup_version_of(data) <= mine:
        return
    if os.path.isfile(os.path.join(target, STATE_FILE)):
        dest = os.path.join(target, SETUP_NAME)
    else:
        dest = os.path.join(os.path.expanduser("~/.cache"), SETUP_NAME)
    _write_file(dest, data, executable=True)
    ver = ".".join(str(x) for x in setup_version_of(data))
    ui.log(T(f"Auf GitHub gibt es eine neuere Version des Installers ({ver}); "
             "sie wird jetzt verwendet.",
             f"There is a newer version of the installer on GitHub ({ver}); it is used now."))
    os.environ[REEXEC_ENV] = "1"
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, dest] + sys.argv[1:])


def stage2_args(args):
    out = []
    if args.target:
        out += ["--target", os.path.abspath(os.path.expanduser(args.target))]
    if args.python_env:
        out += ["--python-env", args.python_env]
    if args.source_zip:
        out += ["--source-zip", os.path.abspath(args.source_zip)]
    if args.lang and args.lang != "auto":
        out += ["--lang", args.lang]
    for flag in ("yes", "no_gui", "no_system_packages", "update", "no_version_check"):
        if getattr(args, flag):
            out.append("--" + flag.replace("_", "-"))
    for opt in ("cohi", "amwave", "samples", "icons", "make", "fl2k"):
        val = getattr(args, opt)
        if val is not None:
            out.append(("--" if val else "--no-") + opt)
    return out


def bootstrap(args):
    ui = ConsoleUI(args.yes)
    if not preflight(ui):
        return 1
    target = resolve_target(args)
    set_log_for(target)
    file_log(f"=== setup stage 1 ({SETUP_VERSION}), {platform.platform()}, "
             f"Python {platform.python_version()}")
    ui.heading(T("COHI-Player Setup", "COHI-Player Setup"))
    self_update(ui, target, args)

    # Voraussetzung für das Fenster der Stufe 2: PyQt5 für dieses Python
    want_gui = not (args.no_gui or args.yes) and display_available()
    if want_gui and not pyqt5_available(sys.executable):
        if args.no_system_packages:
            ui.log(T("PyQt5 fehlt (--no-system-packages)", "PyQt5 missing (--no-system-packages)"))
        else:
            ensure_apt_packages(ui, ["python3-pyqt5"]
                                + (["qttranslations5-l10n"] if GERMAN else []),
                                T("Für das Fenster des Installers wird PyQt5 benötigt.",
                                  "The installer window needs PyQt5."), [])
        if not pyqt5_available(sys.executable):
            ui.log(T("Ohne PyQt5 geht es in der Konsole weiter.",
                     "Without PyQt5 the installer continues in the console."))

    cmd = [sys.executable, os.path.abspath(__file__), "--stage2"] + stage2_args(args)
    if want_gui and pyqt5_available(sys.executable):
        ui.log(T("Das Fenster 'COHI-Player Setup' öffnet sich jetzt (evtl. hinter diesem "
                 "Fenster). Bitte dort weitermachen.",
                 "The window 'COHI-Player Setup' opens now (it may appear behind this "
                 "window). Please continue there."))
    file_log("$ " + " ".join(cmd))
    return subprocess.call(cmd)


# ---------------------------------------------------------------------------
# Stufe 2: Update-Prüfung und Installation (gemeinsam für Konsole und Fenster)
# ---------------------------------------------------------------------------
def player_running():
    try:
        return subprocess.run(["pgrep", "-f", "cohi_player_main.py|cohi_config_gui.py"],
                              stdout=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def fl2k_present():
    return bool(shutil.which("fl2k_file") or os.path.isfile("/usr/local/bin/fl2k_file"))


def option_texts():
    """(Beschriftung, Erklärung) der wählbaren Schritte des COHI-Players"""
    return {
        "icons": (T("Desktop-Icons 'COHI-Player', 'Config COHI' und 'COHI Update' anlegen",
                    "Create the desktop icons 'COHI-Player', 'Config COHI' and 'COHI Update'"),
                  T("Legt die Icons auf dem Desktop und im Startmenü an. "
                    "'COHI Update' holt den neuesten Stand von GitHub.",
                    "Creates the icons on the desktop and in the start menu. "
                    "'COHI Update' fetches the latest version from GitHub.")),
        "make": (T("C++-Tools übersetzen (make clean && make)",
                   "Compile the C++ tools (make clean && make)"),
                 T("Nötig für COHI-Stream, COHI-Player+Stream, FL2K 'cpp' und smiSDR. "
                   "Wird bei jedem Update empfohlen.",
                   "Needed for COHI-Stream, COHI-Player+Stream, FL2K 'cpp' and smiSDR. "
                   "Recommended for every update.")),
        "fl2k": (T("Treiber osmo-fl2k übersetzen und installieren",
                   "Build and install the osmo-fl2k driver")
                 + (T(" (ist bereits installiert)", " (already installed)") if fl2k_present() else ""),
                 T("Nur nötig für den FL2K-USB-VGA-Dongle (README 4.3). "
                   "Für STEMlab und smiSDR nicht erforderlich.",
                   "Only needed for the FL2K USB-VGA dongle (README 4.3). "
                   "Not needed for STEMlab and smiSDR.")),
    }


def env_texts():
    return {
        "system": (T("systemweit (empfohlen für Raspberry Pi)",
                     "system-wide (recommended for Raspberry Pi)")),
        "venv": (T("eigene virtuelle Umgebung <Ziel>/venv (empfohlen für PC)",
                   "own virtual environment <target>/venv (recommended for PC)")),
    }


def check_version(args, state):
    """Vergleich der installierten Version mit GitHub.
    Ergebnis: dict(installed, old, new, uptodate, total, msgs)"""
    vs = {"installed": state.get("installed", ""), "old": state.get("commit", ""),
          "new": "", "uptodate": None, "total": None, "msgs": []}
    if args.source_zip:
        vs["new"] = zip_commit(args.source_zip)
    elif not args.no_version_check:
        vs["new"] = latest_commit()
    if vs["installed"] and vs["old"] and vs["new"]:
        vs["uptodate"] = vs["old"] == vs["new"]
        if not vs["uptodate"] and not args.source_zip:
            vs["total"], vs["msgs"] = changes_since(vs["old"], vs["new"])
    return vs


def version_text(vs):
    """Status des COHI-Players als ein Satz"""
    new = vs["new"][:10]
    if not vs["installed"]:
        return (T(f"Noch nicht installiert. Stand auf GitHub: {new}.",
                  f"Not installed yet. Version on GitHub: {new}.") if new else
                T("Noch nicht installiert.", "Not installed yet."))
    since = T(f"installiert am {vs['installed']}", f"installed {vs['installed']}")
    if not new:
        return T(f"Installiert am {vs['installed']}. GitHub ist nicht erreichbar; der neueste "
                 "Stand wird bei der Installation geladen.",
                 f"Installed {vs['installed']}. GitHub cannot be reached; the latest version "
                 "is downloaded during the installation.")
    if vs["uptodate"]:
        return T(f"Bereits aktuell (Stand {new}, {since}).",
                 f"Already up to date (version {new}, {since}).")
    if vs["total"]:
        return T(f"Update verfügbar: {vs['total']} Änderung(en) seit der Installation "
                 f"({vs['old'][:10]} → {new}, {since}).",
                 f"Update available: {vs['total']} change(s) since the installation "
                 f"({vs['old'][:10]} → {new}, {since}).")
    return T(f"Update verfügbar ({vs['old'][:10]} → {new}, {since}).",
             f"Update available ({vs['old'][:10]} → {new}, {since}).")


def default_config(args, target, state, vs):
    """Vorgaben für alle Auswahlmöglichkeiten; Kommandozeile hat Vorrang"""
    prev = state.get("options", {})
    d = {"cohi": not vs.get("uptodate"),          # schon aktuell: nicht neu installieren
         "target": target,
         "env": state.get("python_env") or default_env(),
         "icons": prev.get("icons", True), "make": True, "fl2k": False,
         "amwave": False, "samples": False}
    for k in ("cohi", "amwave", "samples", "icons", "make", "fl2k"):
        if getattr(args, k) is not None:
            d[k] = getattr(args, k)
    if args.python_env:
        d["env"] = args.python_env
    d["source_zip"] = os.path.abspath(args.source_zip) if args.source_zip else None
    d["no_system_packages"] = args.no_system_packages
    return d


class Progress:
    def __init__(self, ui, total):
        self.ui, self.total, self.done = ui, max(total, 1), 0
        ui.step(0, self.total)

    def next(self):
        self.done += 1
        self.ui.step(min(self.done, self.total), self.total)


def cohi_step_count(cfg):
    return 3 + sum(1 for k in ("make", "fl2k", "icons") if cfg.get(k))


def perform_all(ui, cfg):
    """Die gewählten Programme installieren; True, wenn alles klappte.
    Läuft im Fenster in einem eigenen Thread."""
    total = ((cohi_step_count(cfg) if cfg["cohi"] else 0) + (1 if cfg["amwave"] else 0)
             + (1 if cfg.get("samples") else 0))
    prog = Progress(ui, total)
    manual_steps, results = [], {}
    if cfg["cohi"]:
        try:
            results.update(perform_cohi(ui, cfg, prog, manual_steps))
        except Exception as exc:
            ui.log(T("FEHLER beim COHI-Player: ", "ERROR in COHI-Player: ")
                   + f"{type(exc).__name__}: {exc}")
            results["cohi"] = False
    if cfg["amwave"]:
        try:
            results["amwave"] = install_amwave(ui, manual_steps, cfg["no_system_packages"])
        except Exception as exc:
            ui.log(T("FEHLER bei amwave: ", "ERROR in amwave: ") + f"{type(exc).__name__}: {exc}")
            results["amwave"] = False
        prog.next()
    if cfg.get("samples"):
        # zuletzt, damit die Zielverzeichnisse auch bei einer Erstinstallation existieren
        try:
            results["samples"] = download_samples(ui, cfg.get("sample_plan", []),
                                                  cfg.get("samples_overwrite", False))
        except Exception as exc:
            ui.log(T("FEHLER bei samples: ", "ERROR in samples: ") + f"{type(exc).__name__}: {exc}")
            results["samples"] = False
        prog.next()

    ui.heading(T("Ergebnis", "Result"))
    for step, ok in results.items():
        ui.log(f"  {step:6s}: {'OK' if ok else T('FEHLER', 'FAILED')}")
    ui.log(T(f"Protokoll der Installation: {SETUP_LOG}", f"setup log: {SETUP_LOG}"))
    if manual_steps:
        ui.info(T("Bitte diese Schritte noch von Hand erledigen:\n\n",
                  "Please complete these steps manually:\n\n")
                + "\n".join("- " + s for s in manual_steps))
    return all(results.values())


def perform_cohi(ui, cfg, prog, manual_steps):
    """COHI-Player: laden, Pakete, Dateien, gewählte Schritte"""
    target = os.path.abspath(os.path.expanduser(cfg["target"]))
    env = cfg["env"]
    os.makedirs(target, exist_ok=True)
    set_log_for(target)
    file_log(f"=== COHI-Player nach {target}, Python {env}")
    state = load_state(target)
    results = {}

    # 1. Programmdateien laden
    ui.heading(T("Programmdateien laden", "Downloading the program files"))
    if cfg.get("source_zip"):
        zpath = cfg["source_zip"]
        ui.log(T("verwende ", "using ") + zpath)
    else:
        dl = os.path.join(target, "download")
        os.makedirs(dl, exist_ok=True)
        zpath = os.path.join(dl, f"COHI-Player-Universal-{BRANCH}.zip")
        ui.log(ZIP_URL)
        try:
            http_download(ZIP_URL, zpath, ui.progress)
        except Exception as exc:
            raise RuntimeError(T(f"Download fehlgeschlagen ({exc}). Bitte die "
                                 "Internetverbindung prüfen.",
                                 f"Download failed ({exc}). Please check the internet "
                                 "connection."))
    req_text = read_zip_member(zpath, "requirements.txt").decode("utf-8", "replace")
    commit = zip_commit(zpath)
    if commit:
        ui.log(T("Stand des Repositories: ", "repository version: ") + commit[:10])
    prog.next()

    # 2. Systempakete und Python-Pakete (README 4.1)
    ui.heading(T("Systempakete", "System packages"))
    if cfg.get("no_system_packages"):
        ui.log(T("übersprungen (--no-system-packages)", "skipped (--no-system-packages)"))
    else:
        ensure_apt_packages(ui, base_packages(env),
                            T("COHI-Player braucht einige Systempakete (README 4.1).",
                              "COHI-Player needs some system packages (README 4.1)."),
                            manual_steps)
    ui.heading(T("Python-Pakete", "Python packages"))
    if env == "venv":
        if not create_venv(ui, target):
            raise RuntimeError(T("virtuelle Umgebung fehlt", "virtual environment missing"))
        python = venv_python(target)
    else:
        python = sys.executable
    req = filtered_requirements(req_text, env, os.path.join(target, "requirements_install.txt"))
    # pip läuft bei jedem Lauf: so kommen auch Pakete hinzu, die neu in der
    # requirements.txt stehen (bereits vorhandene kosten nur Sekunden)
    results["python"] = pip_install(ui, python, req, env)
    if not results["python"]:
        manual_steps.append(f"{python} -m pip install -r {req}"
                            + (" --break-system-packages" if env == "system" else ""))
    if not python_ready(python):
        ui.log(T("WARNUNG: nicht alle Python-Pakete lassen sich laden (" + IMPORT_CHECK + ")",
                 "WARNING: not all Python packages can be imported (" + IMPORT_CHECK + ")"))
    state.update({"python_env": env, "python": python, "setup_python": sys.executable})
    save_state(target, state)
    prog.next()

    # 3. Programmdateien kopieren
    ui.heading(T("Programmdateien kopieren", "Copying the program files"))
    app_dir, files = install_files(ui, zpath, target, state.get("files", []))
    # die Dateiliste sofort sichern: sie bestimmt beim nächsten Update,
    # welche Dateien entfernt werden dürfen
    state["files"] = files
    save_state(target, state)
    install_setup_copy(ui, zpath, target)        # Kopie des Installers für Updates
    results["files"] = True
    prog.next()

    # 4. gewählte Schritte
    inst = Installer(ui, target, python, manual_steps, sys.executable)
    actions = {"make": inst.run_make, "fl2k": inst.install_fl2k, "icons": inst.create_launchers}
    for k in ("make", "fl2k", "icons"):
        if not cfg.get(k):
            continue
        try:
            results[k] = actions[k]()
        except Exception as exc:       # ein Schritt darf die anderen nicht verhindern
            ui.log(T(f"FEHLER bei {k}: ", f"ERROR in {k}: ") + f"{type(exc).__name__}: {exc}")
            results[k] = False
        prog.next()

    state.pop("pending_changes", None)
    state.pop("pending_manual_steps", None)
    state.update({"installed": time.strftime("%Y-%m-%d %H:%M:%S"), "commit": commit,
                  "repo": REPO, "branch": BRANCH,
                  "options": {k: bool(cfg.get(k)) for k in ("icons", "make", "fl2k")},
                  "results": results})
    save_state(target, state)

    ui.log(T(f"\nCOHI-Player ist installiert in {app_dir}",
             f"\nCOHI-Player is installed in {app_dir}"))
    ui.log(T("Start: Desktop-Icon 'COHI-Player' oder im Terminal:",
             "Start: desktop icon 'COHI-Player' or in a terminal:")
           + f"\n  cd {app_dir} && {python} cohi_player_main.py\n"
           + T("Grundeinstellungen: Desktop-Icon 'Config COHI' oder:",
               "Basic settings: desktop icon 'Config COHI' or:")
           + f"\n  cd {app_dir} && {python} cohi_config_gui.py")
    return results


# ---------------------------------------------------------------------------
# Stufe 2: Konsole
# ---------------------------------------------------------------------------
def run_stage2_console(args, target, state, vs):
    ui = ConsoleUI(args.yes)
    cfg = default_config(args, target, state, vs)

    ui.heading("COHI-Player")
    ui.log(version_text(vs))
    for m in vs["msgs"]:
        ui.log("  - " + m)
    if args.cohi is None:
        cfg["cohi"] = ui.ask(T("COHI-Player installieren bzw. aktualisieren?",
                               "Install or update COHI-Player?"), cfg["cohi"])
    if cfg["cohi"]:
        if not (args.target or args.update):
            cfg["target"] = os.path.abspath(os.path.expanduser(
                ui.text(T("Installationsverzeichnis", "Installation directory"), target)))
            if cfg["target"] != target:
                cfg["env"] = load_state(cfg["target"]).get("python_env") or cfg["env"]
        if not (args.python_env or args.update):
            cfg["env"] = ui.choice(T("Wie sollen die Python-Pakete installiert werden?",
                                     "How shall the Python packages be installed?"),
                                   list(env_texts().items()), cfg["env"])
        for k, (label, expl) in option_texts().items():
            if getattr(args, k) is None:
                cfg[k] = ui.ask(f"{label}?\n  ({expl})", cfg[k])

    ui.heading(T("Optionale Erweiterungen", "Optional extensions"))
    if args.samples is None:
        cfg["samples"] = ui.ask(T("Beispieldateien herunterladen?\n  (zum Prüfen der Funktion "
                                  "gleich nach der Installation; die Liste wird vorher angezeigt)",
                                  "Download sample files?\n  (to check the function right after "
                                  "the installation; the list is shown first)"), False)
    if args.amwave is None:
        cfg["amwave"] = ui.ask(
            T(f"{AMW_NAME} installieren?\n  (eigenständiges Zusatzprogramm zur Simulation der "
              "Ausbreitungsbedingungen; nur auf einem PC mit großem Bildschirm sinnvoll)",
              f"Install {AMW_NAME}?\n  (independent add-on program simulating the propagation "
              "conditions; only sensible on a PC with a large screen)"), False)
    if cfg["amwave"]:
        for w in amwave_warnings():
            ui.log(T("WARNUNG: ", "WARNING: ") + w)
    if cfg["samples"]:
        cfg["samples"] = confirm_samples_console(ui, cfg)
    if not (cfg["cohi"] or cfg["amwave"] or cfg["samples"]):
        ui.log(T("Nichts ausgewählt, nichts zu tun.", "Nothing selected, nothing to do."))
        return 0

    while cfg["cohi"] and player_running():
        msg = T("COHI-Player oder Config COHI läuft noch und sollte vorher beendet werden.",
                "COHI-Player or Config COHI is still running and should be closed first.")
        if args.yes or not interactive():
            ui.log(msg)
            break
        if not ui.ask(msg + T(" Erneut prüfen?", " Check again?"), True):
            break
        time.sleep(1)
    return 0 if perform_all(ui, cfg) else 2


def confirm_samples_console(ui, cfg):
    """Liste anzeigen und bestätigen lassen; True, wenn heruntergeladen werden soll"""
    ui.log(T("lade die Liste der Beispieldateien ...", "loading the list of sample files ..."))
    try:
        entries, notes = plan_samples(cfg)
    except Exception as exc:
        ui.log(T("FEHLER: Die Liste der Beispieldateien kann nicht geladen werden: ",
                 "ERROR: the list of sample files cannot be loaded: ")
               + str(getattr(exc, "reason", exc)))
        return False
    text, detail = samples_overview(entries, notes, limit=10 ** 6)
    ui.log("\n" + text + "\n")
    if not any(not e["error"] for e in entries):
        return False
    if not ui.ask(T("Beispieldateien herunterladen?", "Download the sample files?"), True):
        return False
    overwrite = False
    existing = existing_samples(entries)
    if existing:
        overwrite = ui.ask(existing_overview(existing), False)
    cfg["sample_plan"], cfg["samples_overwrite"] = entries, overwrite
    return True


# ---------------------------------------------------------------------------
# Stufe 2: Fenster (PyQt5)
# ---------------------------------------------------------------------------
def gui_possible():
    if not display_available():
        return False
    try:
        import importlib
        importlib.import_module("PyQt5.QtWidgets")
        return True
    except Exception:
        return False


def run_gui(args, target, state, vs):
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import Qt

    class Bridge(QtCore.QObject):
        sig_log = QtCore.pyqtSignal(str)
        sig_step = QtCore.pyqtSignal(int, int)
        sig_ask = QtCore.pyqtSignal(str)
        sig_info = QtCore.pyqtSignal(str)

    class GuiUI:
        """Thread-sichere Oberfläche für den Installer, der im Worker-Thread läuft"""
        graphical = True
        assume_yes = False

        def __init__(self, bridge):
            self.bridge = bridge
            self.answer = False

        def log(self, msg):
            print(msg, flush=True)
            file_log(msg)
            self.bridge.sig_log.emit(str(msg))

        def heading(self, msg):
            self.log("\n=== " + msg + " ===")

        def progress(self, done, total):
            pass

        def step(self, done, total):
            self.bridge.sig_step.emit(done, total)

        def ask(self, question, default=True):
            self.bridge.sig_ask.emit(question)        # BlockingQueuedConnection
            self.log(f"{question.splitlines()[0]} -> "
                     f"{T('ja', 'yes') if self.answer else T('nein', 'no')}")
            return self.answer

        def info(self, msg):
            self.log(msg)
            self.bridge.sig_info.emit(msg)

    class Worker(QtCore.QThread):
        sig_done = QtCore.pyqtSignal(bool, str)

        def __init__(self, job):
            super().__init__()
            self.job = job

        def run(self):
            try:
                ok = self.job()
                self.sig_done.emit(bool(ok), "")
            except Exception as exc:
                self.sig_done.emit(False, f"{type(exc).__name__}: {exc}")

    def hint_label(text):
        lab = QtWidgets.QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet("QLabel { color: gray } QLabel:disabled { color: #b8b8b8 }")
        lab.setContentsMargins(22, 0, 0, 4)
        return lab

    class SetupDialog(QtWidgets.QDialog):
        def __init__(self):
            super().__init__()
            self.state = state
            self.vs = vs
            self.worker = None
            self.ok = False
            self.ran_cohi = False
            cfg = default_config(args, target, state, vs)
            self.setWindowTitle("COHI-Player Setup")
            icon = os.path.join(target, "icons", os.path.basename(ICON_REPO_PATH))
            if os.path.isfile(icon):
                self.setWindowIcon(QtGui.QIcon(icon))
            outer = QtWidgets.QVBoxLayout(self)
            content = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(content)
            lay.setContentsMargins(0, 0, 0, 0)

            # --- Block COHI-Player ----------------------------------------------
            self.grp_cohi = QtWidgets.QGroupBox("COHI-Player")
            self.grp_cohi.setCheckable(True)
            self.grp_cohi.setChecked(cfg["cohi"])
            self.grp_cohi.toggled.connect(lambda _on: self.refresh_cohi_status())
            g = QtWidgets.QVBoxLayout(self.grp_cohi)
            form = QtWidgets.QFormLayout()
            row = QtWidgets.QHBoxLayout()
            self.ed_target = QtWidgets.QLineEdit(cfg["target"])
            self.ed_target.editingFinished.connect(self.target_changed)
            bt_browse = QtWidgets.QPushButton(T("Ändern ...", "Change ..."))
            bt_browse.clicked.connect(self.browse_target)
            row.addWidget(self.ed_target, 1)
            row.addWidget(bt_browse)
            form.addRow(T("Installationsverzeichnis:", "Installation directory:"), row)
            envbox = QtWidgets.QVBoxLayout()
            self.rb_env = {}
            for k, text in env_texts().items():
                rb = QtWidgets.QRadioButton(text)
                rb.setChecked(k == cfg["env"])
                envbox.addWidget(rb)
                self.rb_env[k] = rb
            form.addRow(T("Python-Pakete:", "Python packages:"), envbox)
            self.lb_version = QtWidgets.QLabel()
            self.lb_version.setWordWrap(True)
            form.addRow(T("Version:", "Version:"), self.lb_version)
            g.addLayout(form)
            base = QtWidgets.QCheckBox(T("Programmdateien laden und kopieren, Python-Pakete "
                                         "installieren  (immer)",
                                         "Download and copy the program files, install the "
                                         "Python packages  (always)"))
            base.setChecked(True)
            base.setEnabled(False)
            g.addWidget(base)
            self.cb = {}
            for k, (label, expl) in option_texts().items():
                cb = QtWidgets.QCheckBox(label.replace("&", "&&"))
                cb.setChecked(cfg[k])
                cb.setToolTip(expl)
                g.addWidget(cb)
                g.addWidget(hint_label(expl))
                self.cb[k] = cb
            lay.addWidget(self.grp_cohi)

            # --- Block Optionale Erweiterungen -------------------------------------
            grp_ext = QtWidgets.QGroupBox(T("Optionale Erweiterungen", "Optional extensions"))
            ge = QtWidgets.QVBoxLayout(grp_ext)
            self.cb_samples = QtWidgets.QCheckBox(T("Beispieldateien herunterladen",
                                                    "Download sample files"))
            self.cb_samples.setChecked(cfg["samples"])
            ge.addWidget(self.cb_samples)
            ge.addWidget(hint_label(
                T("Lädt die in installer/sample_files.txt im GitHub-Repository aufgeführten "
                  "Beispieldateien, um die Funktion gleich nach der Installation prüfen zu "
                  "können. Die Dateien werden vorher angezeigt; vorhandene Dateien werden nur "
                  "nach Rückfrage ersetzt und bleiben als Sicherung erhalten.",
                  "Downloads the sample files listed in installer/sample_files.txt in the "
                  "GitHub repository, to check the function right after the installation. "
                  "The files are shown first; existing files are only replaced after asking "
                  "and are kept as a backup.")))
            self.cb_amw = QtWidgets.QCheckBox(AMW_NAME)
            self.cb_amw.setChecked(cfg["amwave"])
            self.cb_amw.clicked.connect(self.on_amwave_clicked)
            ge.addWidget(self.cb_amw)
            ge.addWidget(hint_label(
                T(f"Eigenständiges Zusatzprogramm zur Simulation der Ausbreitungsbedingungen, "
                  f"installiert nach {amwave_dir()} mit eigener virtueller Umgebung (uv, Python "
                  f"{AMW_PYTHON}) und Desktop-Icon. Nur auf einem PC mit großem Bildschirm "
                  "sinnvoll. Eine vorhandene Installation wird überschrieben.",
                  f"Independent add-on program simulating the propagation conditions, installed "
                  f"to {amwave_dir()} with its own virtual environment (uv, Python {AMW_PYTHON}) "
                  "and desktop icon. Only sensible on a PC with a large screen. An existing "
                  "installation is overwritten.")))
            self.grp_ext = grp_ext
            lay.addWidget(grp_ext)
            lay.addStretch(1)

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setWidget(content)

            self.log = QtWidgets.QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(20000)
            self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            split = QtWidgets.QSplitter(Qt.Vertical)
            split.addWidget(scroll)
            split.addWidget(self.log)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 1)
            outer.addWidget(split, 1)
            self.split = split

            self.bar = QtWidgets.QProgressBar()
            self.bar.setValue(0)
            outer.addWidget(self.bar)
            btns = QtWidgets.QHBoxLayout()
            self.bt_install = QtWidgets.QPushButton()
            self.bt_install.setDefault(True)
            self.bt_install.clicked.connect(self.start)
            self.bt_config = QtWidgets.QPushButton(T("Config COHI starten", "Start Config COHI"))
            self.bt_config.clicked.connect(lambda: self.launch("cohi_config_gui.py"))
            self.bt_player = QtWidgets.QPushButton(T("COHI-Player starten", "Start COHI-Player"))
            self.bt_player.clicked.connect(lambda: self.launch("cohi_player_main.py"))
            self.bt_amw = QtWidgets.QPushButton(T("AMWaveSynth starten", "Start AMWaveSynth"))
            self.bt_amw.clicked.connect(self.launch_amwave)
            self.bt_close = QtWidgets.QPushButton(T("Schließen", "Close"))
            self.bt_close.clicked.connect(self.close)
            btns.addWidget(self.bt_install)
            btns.addStretch(1)
            for b in (self.bt_config, self.bt_player, self.bt_amw, self.bt_close):
                btns.addWidget(b)
            outer.addLayout(btns)

            self.bridge = Bridge()
            self.gui_ui = GuiUI(self.bridge)
            self.bridge.sig_log.connect(self.log.appendPlainText)
            self.bridge.sig_step.connect(self.set_step)
            self.bridge.sig_ask.connect(self.on_ask, Qt.BlockingQueuedConnection)
            self.bridge.sig_info.connect(self.on_info)

            self.refresh_cohi_status()
            if vs["msgs"]:
                self.log.appendPlainText(T("Neu seit der letzten Installation:",
                                           "New since the last installation:"))
                for m in vs["msgs"]:
                    self.log.appendPlainText("  - " + m)
                if vs["total"] and vs["total"] > len(vs["msgs"]):
                    self.log.appendPlainText("  ...")
                self.log.appendPlainText("")
            self.log.appendPlainText(T("Bitte die gewünschten Punkte auswählen und auf "
                                       f"'{self.bt_install.text()}' klicken.",
                                       "Please select the desired items and click "
                                       f"'{self.bt_install.text()}'."))
            self.log.moveCursor(QtGui.QTextCursor.Start)

            # an den Bildschirm anpassen (auch kleine Displays)
            avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
            w, h = min(820, avail.width()), min(880, avail.height())
            self.resize(w, h)
            split.setSizes([int(h * 0.68), int(h * 0.22)])

        # --- Hilfen -------------------------------------------------------------
        def cur_target(self):
            return os.path.abspath(os.path.expanduser(self.ed_target.text().strip()
                                                      or default_target()))

        def cur_state(self):
            return load_state(self.cur_target())

        def app_dir(self):
            return os.path.join(self.cur_target(), APP_SUBDIR)

        def refresh_cohi_status(self):
            """Versionszeile, Beschriftung und Start-Schaltflächen zum gewählten Verzeichnis"""
            st = self.cur_state()
            v = dict(self.vs)
            v["installed"], v["old"] = st.get("installed", ""), st.get("commit", "")
            if v["installed"] and v["old"] and v["new"]:
                same = v["old"] == v["new"]
                if same != bool(self.vs.get("uptodate")) or v["old"] != self.vs.get("old"):
                    v["total"], v["msgs"] = None, []
                v["uptodate"] = same
            else:
                v["uptodate"] = None
            self.lb_version.setText(version_text(v))
            if not self.grp_cohi.isChecked():
                self.bt_install.setText(T("Ausführen", "Run"))
            else:
                self.bt_install.setText(T("Aktualisieren", "Update") if v["installed"]
                                        else T("Installieren", "Install"))
            self.set_launch_buttons()

        def target_changed(self):
            st = self.cur_state()
            if st.get("python_env") in self.rb_env:
                self.rb_env[st["python_env"]].setChecked(True)
            self.refresh_cohi_status()

        def browse_target(self):
            start = self.cur_target()
            if not os.path.isdir(start):
                start = os.path.expanduser("~")
            d = QtWidgets.QFileDialog.getExistingDirectory(
                self, T("Installationsverzeichnis wählen", "Choose installation directory"), start)
            if d:
                self.ed_target.setText(d)
                self.target_changed()

        def set_launch_buttons(self):
            idle = self.worker is None
            st = self.cur_state()
            ready = os.path.isfile(os.path.join(self.app_dir(), "cohi_player_main.py")) \
                and bool(st.get("python"))
            self.bt_config.setEnabled(ready and idle)
            self.bt_player.setEnabled(ready and idle)
            self.bt_amw.setVisible(amwave_installed() and os.path.isfile(amwave_python()))
            self.bt_amw.setEnabled(idle)

        def on_amwave_clicked(self, checked):
            """Warnungen beim Anhaken: Raspberry Pi, vorhandene Installation"""
            if not checked:
                return
            warnings = amwave_warnings()
            if warnings:
                QtWidgets.QMessageBox.warning(self, "COHI-Player Setup", "\n\n".join(warnings))

        def set_step(self, done, total):
            self.bar.setMaximum(max(total, 1))
            self.bar.setValue(done)
            self.bar.setFormat(f"{done}/{total}")

        def on_ask(self, question):
            r = QtWidgets.QMessageBox.question(self, "COHI-Player Setup", question,
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                               QtWidgets.QMessageBox.Yes)
            self.gui_ui.answer = r == QtWidgets.QMessageBox.Yes

        def on_info(self, msg):
            QtWidgets.QMessageBox.information(self, "COHI-Player Setup", msg)

        # --- Installation ---------------------------------------------------------
        def start(self):
            cfg = default_config(args, self.cur_target(), self.cur_state(), self.vs)
            cfg["cohi"] = self.grp_cohi.isChecked()
            cfg["target"] = self.cur_target()
            cfg["env"] = next(k for k, rb in self.rb_env.items() if rb.isChecked())
            for k, cb in self.cb.items():
                cfg[k] = cb.isChecked()
            cfg["amwave"] = self.cb_amw.isChecked()
            cfg["samples"] = self.cb_samples.isChecked()
            if not (cfg["cohi"] or cfg["amwave"] or cfg["samples"]):
                QtWidgets.QMessageBox.information(self, "COHI-Player Setup",
                                                  T("Es ist nichts ausgewählt.",
                                                    "Nothing is selected."))
                return
            if cfg["cohi"]:
                try:
                    os.makedirs(cfg["target"], exist_ok=True)
                except OSError as exc:
                    QtWidgets.QMessageBox.critical(self, "COHI-Player Setup",
                                                   f"{cfg['target']}: {exc}")
                    return
            while cfg["cohi"] and player_running():
                r = QtWidgets.QMessageBox.warning(
                    self, "COHI-Player Setup",
                    T("COHI-Player oder Config COHI läuft noch. Bitte zuerst beenden.",
                      "COHI-Player or Config COHI is still running. Please close it first."),
                    QtWidgets.QMessageBox.Retry | QtWidgets.QMessageBox.Ignore
                    | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Retry)
                if r == QtWidgets.QMessageBox.Cancel:
                    return
                if r == QtWidgets.QMessageBox.Ignore:
                    break
            if cfg["samples"]:
                plan = self.confirm_samples(cfg)
                if plan is None:                       # Abbrechen
                    return
                cfg["sample_plan"], cfg["samples_overwrite"] = plan
                cfg["samples"] = bool(plan[0])
                if not (cfg["cohi"] or cfg["amwave"] or cfg["samples"]):
                    return
            self.ran_cohi = cfg["cohi"]
            for w in (self.grp_cohi, self.grp_ext, self.bt_install, self.bt_close):
                w.setEnabled(False)
            self.worker = Worker(lambda: perform_all(self.gui_ui, cfg))
            self.set_launch_buttons()
            self.worker.sig_done.connect(self.finished)
            self.worker.start()

        def confirm_samples(self, cfg):
            """Liste laden und anzeigen, nach dem Überschreiben fragen.
            Ergebnis: None = Abbrechen, ([], False) = ohne Beispieldateien weiter,
            sonst (Einträge, überschreiben)"""
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                entries, notes = plan_samples(cfg)
            except Exception as exc:
                QtWidgets.QApplication.restoreOverrideCursor()
                QtWidgets.QMessageBox.critical(
                    self, "COHI-Player Setup",
                    T("Die Liste der Beispieldateien kann nicht geladen werden:\n\n",
                      "The list of sample files cannot be loaded:\n\n")
                    + f"{SAMPLES_RAW_URL}\n{getattr(exc, 'reason', exc)}")
                return ([], False)
            QtWidgets.QApplication.restoreOverrideCursor()
            text, detail = samples_overview(entries, notes)
            if not any(not e["error"] for e in entries):
                QtWidgets.QMessageBox.warning(self, "COHI-Player Setup", text)
                return ([], False)
            Box = QtWidgets.QMessageBox
            width = min(680, int(QtWidgets.QApplication.primaryScreen()
                                 .availableGeometry().width() * 0.8))
            wide = f"QLabel#qt_msgbox_label {{ min-width: {width}px; }}"
            mb = Box(Box.Question, "COHI-Player Setup", text, Box.Yes | Box.No | Box.Cancel, self)
            mb.setStyleSheet(wide)
            mb.button(Box.Yes).setText(T("Herunterladen", "Download"))
            mb.button(Box.No).setText(T("Ohne Beispieldateien", "Without sample files"))
            mb.setDefaultButton(Box.Yes)
            if detail:
                mb.setDetailedText(detail)
            r = mb.exec_()
            if r == Box.Cancel:
                return None
            if r == Box.No:
                return ([], False)
            overwrite = False
            existing = existing_samples(entries)
            if existing:
                mb = Box(Box.Question, "COHI-Player Setup", existing_overview(existing),
                         Box.Yes | Box.No | Box.Cancel, self)
                mb.setStyleSheet(wide)
                mb.button(Box.Yes).setText(T("Überschreiben", "Overwrite"))
                mb.button(Box.No).setText(T("Nur fehlende laden", "Only missing ones"))
                mb.setDefaultButton(Box.No)
                r = mb.exec_()
                if r == Box.Cancel:
                    return None
                overwrite = r == Box.Yes
            return (entries, overwrite)

        def finished(self, ok, err):
            self.worker = None
            for w in (self.grp_cohi, self.grp_ext, self.bt_install, self.bt_close):
                w.setEnabled(True)
            if self.ran_cohi:
                # der gerade installierte Stand ist jetzt der aktuelle
                self.vs = dict(self.vs, msgs=[], total=None)
                if not self.vs.get("new"):
                    self.vs["new"] = self.cur_state().get("commit", "")
            self.refresh_cohi_status()
            if err:
                self.log.appendPlainText(T("FEHLER: ", "ERROR: ") + err)
                file_log("ERROR: " + err)
                QtWidgets.QMessageBox.critical(self, "COHI-Player Setup",
                                               T("Die Installation ist fehlgeschlagen:\n\n",
                                                 "The installation failed:\n\n") + err)
                return
            self.ok = ok
            text = (T("Die Installation ist abgeschlossen.", "The installation is complete.")
                    if ok else
                    T("Die Installation ist abgeschlossen, aber nicht alle Schritte waren "
                      "erfolgreich (siehe Protokoll).",
                      "The installation is complete, but not all steps were successful "
                      "(see the log)."))
            if not self.ran_cohi:
                QtWidgets.QMessageBox.information(self, "COHI-Player Setup", text)
                return
            r = QtWidgets.QMessageBox.question(
                self, "COHI-Player Setup",
                text + "\n\n" + T("Jetzt 'Config COHI' starten, um Ausgabe-Hardware, "
                                  "Programmart und Display einzustellen?",
                                  "Start 'Config COHI' now to set output hardware, "
                                  "program mode and display?"),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.Yes)
            if r == QtWidgets.QMessageBox.Yes:
                self.launch("cohi_config_gui.py")

        def launch_amwave(self):
            self.launch(AMW_MAIN, python=amwave_python(), cwd=amwave_dir())

        def launch(self, script, python=None, cwd=None):
            """Programm unabhängig vom Installer starten (Arbeitsverzeichnis =
            Programmverzeichnis, dort liegt config_sdr.yaml)"""
            python = python or self.cur_state().get("python") or sys.executable
            try:
                subprocess.Popen([python, script], cwd=cwd or self.app_dir(),
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log.appendPlainText(T("gestartet: ", "started: ") + script)
            except OSError as exc:
                QtWidgets.QMessageBox.critical(self, "COHI-Player Setup", f"{script}: {exc}")

        def closeEvent(self, ev):
            if self.worker is not None:
                ev.ignore()
            else:
                ev.accept()

    app = QtWidgets.QApplication(sys.argv[:1])
    if GERMAN:
        # Ja/Nein, Abbrechen, Dateiauswahl usw. auf Deutsch (qttranslations5-l10n
        # bzw. die Übersetzungen im PyQt5-Wheel)
        translator = QtCore.QTranslator(app)
        dirs = [QtCore.QLibraryInfo.location(QtCore.QLibraryInfo.TranslationsPath),
                "/usr/share/qt5/translations"]
        if any(translator.load("qtbase_de", d) for d in dirs if d):
            app.installTranslator(translator)
    else:
        QtCore.QLocale.setDefault(QtCore.QLocale(QtCore.QLocale.English))
    dlg = SetupDialog()
    # aus einem Terminal gestartet öffnet das Fenster sonst oft dahinter
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()

    def release_on_top():
        dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowStaysOnTopHint)
        dlg.show()
    QtCore.QTimer.singleShot(1500, release_on_top)
    app.exec_()
    return 0 if dlg.ok else 2


def run_stage2(args):
    target = resolve_target(args)
    set_log_for(target)
    file_log(f"=== setup stage 2 ({SETUP_VERSION}), Python {platform.python_version()} "
             f"({sys.executable})")
    state = load_state(target)
    if state.get("installed") and not args.source_zip and not args.no_version_check:
        print(T("Prüfe auf Updates des COHI-Players ...", "Checking for COHI-Player updates ..."),
              flush=True)
    vs = check_version(args, state)
    if not args.no_gui and not args.yes:
        if gui_possible():
            return run_gui(args, target, state, vs)
        print(T("Kein Fenster möglich (kein Desktop oder PyQt5 fehlt), weiter in der Konsole.",
                "No window possible (no desktop or PyQt5 missing), continuing in the console."))
    return run_stage2_console(args, target, state, vs)


# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description=T("Installationsprogramm für COHI-Player-Universal",
                                               "Installer for COHI-Player-Universal"))
    ap.add_argument("--target", help=T("Installationsverzeichnis (Vorgabe ~/COHIRADIA)",
                                       "installation directory (default ~/COHIRADIA)"))
    ap.add_argument("--python-env", choices=("system", "venv"),
                    help=T("Python-Pakete systemweit oder in <Ziel>/venv",
                           "Python packages system-wide or in <target>/venv"))
    ap.add_argument("--cohi", action=argparse.BooleanOptionalAction, default=None,
                    help=T("COHI-Player installieren/aktualisieren (Vorgabe: ja, außer er "
                           "ist schon aktuell)",
                           "install/update COHI-Player (default: yes, unless already up to date)"))
    ap.add_argument("--amwave", action=argparse.BooleanOptionalAction, default=None,
                    help=T(f"{AMW_NAME} nach ~/{AMW_NAME} installieren (Vorgabe: nein)",
                           f"install {AMW_NAME} to ~/{AMW_NAME} (default: no)"))
    ap.add_argument("--samples", action=argparse.BooleanOptionalAction, default=None,
                    help=T("Beispieldateien aus installer/sample_files.txt herunterladen "
                           "(Vorgabe: nein)",
                           "download the sample files listed in installer/sample_files.txt "
                           "(default: no)"))
    ap.add_argument("--icons", action=argparse.BooleanOptionalAction, default=None,
                    help=T("Desktop-Icons anlegen", "create desktop icons"))
    ap.add_argument("--make", action=argparse.BooleanOptionalAction, default=None,
                    help=T("make clean && make ausführen", "run make clean && make"))
    ap.add_argument("--fl2k", action=argparse.BooleanOptionalAction, default=None,
                    help=T("osmo-fl2k bauen und installieren", "build and install osmo-fl2k"))
    ap.add_argument("--lang", choices=LANG_CHOICES, default="auto",
                    help=T("Sprache: auto = nach der Spracheinstellung des Systems "
                           "(Deutsch oder Englisch), de = Deutsch, en = Englisch",
                           "language: auto = from the system language setting "
                           "(German or English), de = German, en = English"))
    ap.add_argument("--no-gui", action="store_true",
                    help=T("Stufe 2 in der Konsole statt im Fenster",
                           "stage 2 in the console instead of a window"))
    ap.add_argument("--yes", action="store_true",
                    help=T("keine Rückfragen, Vorgaben verwenden (Konsole)",
                           "no questions, use defaults (console)"))
    ap.add_argument("--source-zip", help=T("ZIP des Repositories statt Download",
                                           "ZIP of the repository instead of downloading"))
    ap.add_argument("--no-system-packages", action="store_true",
                    help=T("Systempakete (apt) nicht prüfen", "do not check system packages (apt)"))
    ap.add_argument("--update", action="store_true",
                    help=T("vorhandene Installation aktualisieren (Verzeichnis und "
                           "Python-Variante wie bisher)",
                           "update the existing installation (directory and Python "
                           "variant as before)"))
    ap.add_argument("--no-version-check", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--stage2", action="store_true", help=argparse.SUPPRESS)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    set_language(args.lang)
    prefer_ipv4()
    try:
        if args.stage2:
            return run_stage2(args)
        rc = bootstrap(args)
    except KeyboardInterrupt:
        print(T("\nabgebrochen", "\naborted"))
        rc = 130
    # das Icon "COHI Update" öffnet ein Terminal, das sich sonst sofort schließt
    if args.update and interactive():
        try:
            input(T("\nEnter drücken zum Schließen ...", "\nPress Enter to close ..."))
        except (EOFError, KeyboardInterrupt):
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
