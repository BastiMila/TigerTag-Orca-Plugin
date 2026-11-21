"""Build the presets-only edition of the plugin: one .py for publishing on Orca Cloud.

    python tools/build_presets_edition.py [out file]   (default dist/tigertag_presets.py)

It holds only "TigerTag: sync filament presets": sign in, read the inventory, write
the presets. No filament slots, no printers, no TigerTag tab, no load request -
it reads the account and writes nothing to it.

Made from tigertag_orca_plugin.py, which stays the one source:
- lines between "# region printer" and "# endregion printer" are dropped;
- lines between "# region presets-only" and "# endregion presets-only" are kept
  with their comment sign removed ("# x" -> "x", indentation kept);
- the PEP 723 header and the module docstring are replaced by the ones below.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "tigertag_orca_plugin.py")
DEFAULT_OUT = os.path.join(REPO, "dist", "tigertag_presets.py")

VERSION = "0.1.0"

HEADER = f'''# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "TigerTag Filament Presets"
# description = "Creates OrcaSlicer filament presets from the spools of a TigerTag account."
# author = "BastiMila"
# version = "{VERSION}"
# ///
"""TigerTag Filament Presets for OrcaSlicer - one filament preset per spool.

Run "TigerTag: sync filament presets" from the Plugins window. The plugin signs in
to the TigerTag account, reads users/{{uid}}/inventory and writes one filament preset
per spool into the local preset bundle "tigertag"
(<data_dir>/user/<preset folder>/_local/tigertag/), then asks Orca to reload it.

Every preset inherits an installed "@System" filament preset (brand + series
first, then "Generic <material>") and overrides only what the spool knows:
vendor, colour, diameter, density, the nozzle and bed temperature ranges, and a
note with the chip UID and the filament left.

The plugin only reads the account; it writes nothing to it.

Built by tools/build_presets_edition.py from tigertag_orca_plugin.py;
edit that file, not this one.
"""
'''

_REGION = re.compile(r"^\s*# (region|endregion) (printer|presets-only)\s*$")


def build(text):
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("import "))
    out, region = [], None
    for number, line in enumerate(lines[start:], start + 1):
        marker = _REGION.match(line)
        if marker:
            kind, name = marker.groups()
            if kind == "region":
                if region:
                    raise ValueError(f"line {number}: region {name} inside region {region}")
                region = name
            else:
                if region != name:
                    raise ValueError(f"line {number}: endregion {name} without its region")
                region = None
            continue
        if region == "printer":
            continue
        if region == "presets-only":
            line = re.sub(r"^(\s*)# ?", r"\1", line, count=1)
        out.append(line)
    if region:
        raise ValueError(f"region {region} is not closed")
    body = re.sub(r'^PLUGIN_VERSION = ".*"$', f'PLUGIN_VERSION = "{VERSION}"', "".join(out),
                  count=1, flags=re.MULTILINE)
    body = re.sub(r"\n{4,}", "\n\n\n", body)  # gaps left by dropped blocks
    return HEADER + body


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    with open(SOURCE, encoding="utf-8") as handle:
        text = build(handle.read())
    compile(text, out, "exec")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(out)


if __name__ == "__main__":
    main()
