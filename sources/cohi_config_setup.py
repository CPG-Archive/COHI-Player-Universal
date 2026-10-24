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

try:
    import yaml
except ImportError:
    print(tr("Error: PyYAML is not installed."))
    print(tr("Install with: pip install pyyaml --break-system-packages"))
    sys.exit(1)

from yaml_edit_utils import replace_yaml_value


def hwconfig_options():
    return [
        ("stemlab", tr("STEMlab (Red Pitaya)")),
        ("fl2k", tr("FL2K (USB VGA dongle as improvised RF transmitter)")),
    ]


def program_variant_options():
    return [
        ("COHI-Player", tr("WAV playback only (historical recordings)")),
        ("COHI-Stream", tr("internet live streams only")),
        ("COHI-Player+Stream", tr("both at once (WAV + live streams)")),
    ]


def display_variant_options():
    return [
        (1, tr('3.5" small display (e.g. Raspberry Pi 4 with touchscreen)')),
        (2, tr('7" or larger')),
    ]


def language_options():
    return [
        ("en", "English"),
        ("de", "Deutsch"),
        ("auto", tr("automatic (follow the system language)")),
    ]

LIVE_VARIANTS = ("COHI-Stream", "COHI-Player+Stream")


def current_language_setting(current):
    raw = str(current.get("language", "en")).strip().lower()
    return raw if raw in ("en", "de", "auto") else "en"


def read_file(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        print(tr("Error while reading {file}: {error}").format(file=path, error=exc))
        sys.exit(1)


def parse_current_values(text, path):
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        print("\n" + tr("Warning: {file} is not valid YAML ({error})").format(file=path, error=exc))
        print(tr("Current values cannot be read reliably - defaults are assumed. "
                 "Please check the file carefully after saving."))
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def ask_choice(title, options, current_value):
    print(f"\n--- {title} ---")
    for i, (value, description) in enumerate(options, start=1):
        marker = tr("  <- current") if value == current_value else ""
        print(f"  {i}) {value} - {description}{marker}")
    while True:
        raw = input(tr("Selection [1-{count}, Enter = keep unchanged]: ").format(count=len(options))).strip()
        if raw == "":
            return current_value
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(tr("  Please enter a number between 1 and {count} (or press Enter).").format(count=len(options)))


def ask_yes_no(question, default_yes=True):
    suffix = tr("[Y/n]") if default_yes else tr("[y/N]")
    raw = input(f"{question} {suffix}: ").strip().lower()
    if raw == "":
        return default_yes
    return raw in ("j", "ja", "y", "yes")


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config_sdr.yaml")
    i18n.init_from_config(str(config_path))

    print("=" * 64)
    print(" " + tr("COHI-Player Configuration Assistant"))
    print("=" * 64)

    if not config_path.exists():
        print("\n" + tr("{file} was not found.").format(file=config_path))
        print(tr("Please run this in the COHI-Player installation directory, "
                 "or pass the path as an argument:"))
        print("  python3 cohi_config_setup.py /path/to/config_sdr.yaml")
        sys.exit(1)

    original_text = read_file(config_path)
    current = parse_current_values(original_text, config_path)

    current_hwconfig = current.get("hwconfig", "fl2k")
    current_program_variant = current.get("program_variant", "COHI-Player")
    current_display_variant = current.get("display_variant", 1)
    current_fl2k_upconverter = current.get("fl2k_upconverter", "ffmpeg")
    current_language = current_language_setting(current)

    print("\n" + tr("Current configuration in {file}:").format(file=config_path))
    print(f"  hwconfig:         {current_hwconfig}")
    print(f"  program_variant:  {current_program_variant}")
    print(f"  display_variant:  {current_display_variant}")
    print(f"  language:         {current_language}")

    new_hwconfig = ask_choice(tr("Step 1/4: Output hardware"), hwconfig_options(), current_hwconfig)
    new_program_variant = ask_choice(tr("Step 2/4: Operating mode"), program_variant_options(),
                                      current_program_variant)
    new_display_variant = ask_choice(tr("Step 3/4: Display size"), display_variant_options(),
                                      current_display_variant)
    new_language = ask_choice(tr("Step 4/4: Language (takes effect after restart)"),
                               language_options(), current_language)

    new_fl2k_upconverter = current_fl2k_upconverter
    fl2k_upconverter_hint = None
    if new_hwconfig == "fl2k" and new_program_variant in LIVE_VARIANTS:
        if current_fl2k_upconverter != "cpp":
            print("\n" + tr("Note: {variant!r} with hwconfig: fl2k requires "
                           "fl2k_upconverter: cpp (current: {current!r}).").format(
                variant=new_program_variant, current=current_fl2k_upconverter))
            if ask_yes_no(tr("Set it to 'cpp' automatically?"), default_yes=True):
                new_fl2k_upconverter = "cpp"
            else:
                fl2k_upconverter_hint = tr(
                    "WARNING: fl2k_upconverter stays at {current!r} - {variant!r} will NOT "
                    "work with it. Please check fl2k_upconverter manually in {file} afterwards."
                ).format(current=current_fl2k_upconverter, variant=new_program_variant,
                         file=config_path)

    stemlab_live_hint = None
    if new_hwconfig == "stemlab" and new_program_variant == "COHI-Stream":
        has_lo = current.get("stemlab_live_lo_freq") is not None
        has_rate = current.get("stemlab_live_sample_rate") is not None
        if not (has_lo and has_rate):
            stemlab_live_hint = tr(
                "NOTE: hwconfig: stemlab + program_variant: COHI-Stream additionally requires "
                "stemlab_live_lo_freq and stemlab_live_sample_rate in {file} - this tool does NOT "
                "set these values (they depend on the actual station list). Please add them "
                "manually afterwards - see the comments at the relevant place in the file."
            ).format(file=config_path)

    print("\n" + "=" * 64)
    print(" " + tr("Summary"))
    print("=" * 64)
    pending = [
        ("hwconfig", current_hwconfig, new_hwconfig),
        ("program_variant", current_program_variant, new_program_variant),
        ("display_variant", current_display_variant, new_display_variant),
        ("fl2k_upconverter", current_fl2k_upconverter, new_fl2k_upconverter),
        ("language", current_language, new_language),
    ]
    changed_keys = [label for label, old, new in pending if str(old) != str(new)]
    for label, old, new in pending:
        if str(old) != str(new):
            print(f"  {label}: {old}  ->  {new}")
        else:
            print("  " + tr("{label}: {old}  (unchanged)").format(label=label, old=old))

    if fl2k_upconverter_hint:
        print(f"\n{fl2k_upconverter_hint}")
    if stemlab_live_hint:
        print(f"\n{stemlab_live_hint}")

    if not changed_keys:
        print("\n" + tr("No changes were made."))
        return

    print()
    if not ask_yes_no(tr("Save {file} now with these changes?").format(file=config_path), default_yes=False):
        print(tr("Cancelled, no changes saved."))
        return

    new_text = original_text
    for key, new_value in (
        ("hwconfig", new_hwconfig),
        ("program_variant", new_program_variant),
        ("display_variant", new_display_variant),
        ("fl2k_upconverter", new_fl2k_upconverter),
        ("language", new_language),
    ):
        result = replace_yaml_value(new_text, key, new_value)
        if result is None and key == "language":
            result = new_text.rstrip("\n") + f"\n\nlanguage: {new_value}\n"
        if result is None:
            print(tr("Warning: key '{key}:' was not found in the file - please add "
                     "'{key}: {value}' manually.").format(key=key, value=new_value))
            continue
        new_text = result

    backup_path = config_path.with_suffix(config_path.suffix + ".bak")
    try:
        backup_path.write_text(original_text, encoding="utf-8")
        config_path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        print("\n" + tr("Error while writing: {error}").format(error=exc))
        sys.exit(1)

    print("\n" + tr("Saved. Backup copy of the previous version: {file}").format(file=backup_path))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n" + tr("Cancelled."))
        sys.exit(1)
