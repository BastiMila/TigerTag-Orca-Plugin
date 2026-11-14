# TigerTag for OrcaSlicer

An OrcaSlicer 2.5 plugin that brings the spools of your
[TigerTag](https://github.com/TigerTag-Project) account into the slicer.

**Status: filament presets work in OrcaSlicer 2.5.0-rc; slot loading is
written but not yet tested.**

1. **Filament presets from your spools**: one preset per spool, with
   brand, colour, diameter, density and the nozzle and bed temperature ranges
   read from the spool. Each preset inherits a matching OrcaSlicer system
   preset, such as `SUNLU PLA+ @System` or `Generic PETG @System`.
2. **The right preset in each filament slot** (written, untested): when a
   TigerSpool loaded a spool into your printer, the sync puts that spool's
   preset into the matching filament slot of the selected printer.
3. A TigerTag tab listing your spools and what is left on them, with a warning
   when a job needs more than the spool holds (planned).

The plugin only reads your account. It never changes it.

## Install

1. OrcaSlicer: Plugins window, *Install local plugin*, pick `tigertag_orca_plugin.py`.
   Allow network access for the TigerTag and Google hosts when asked.
2. In the plugin's Config tab, enter your TigerTag `email` and `password`.
   The password is used once. Afterwards the plugin keeps only a session
   token, in `tigertag_plugin/.session.json` in OrcaSlicer's data folder, so
   it survives a reinstall of the plugin.
3. Run *TigerTag: sync filament presets*. Run it again whenever your spools change.

The presets land in the local preset bundle `tigertag`, which each sync
replaces. Your edits are kept: saving an edited TigerTag preset creates your
own copy, which the sync never touches. Unsaved edits stay in place, too.
When a TigerSpool reports a spool in a slot, your copy is loaded instead of
the TigerTag preset.

The sync works out which printer in your TigerTag account the selected
OrcaSlicer printer is, and remembers it under `printers` in the Config tab.
If it cannot tell, it says so; then add `"<OrcaSlicer printer preset name>":
"<printer id>"` there yourself.

## Development

    python -m unittest discover -s tests -v
    python -I tools/dry_run.py LOCAL.md <out dir>   # sync against a real account, outside Orca

To test slot loading without a printer or a TigerSpool, put a `test_slots.json`
(shaped like `tools/test_slots.example.json`) into the plugin's folder in
OrcaSlicer's `orca_plugins`. While it exists, the sync uses it instead of what
the TigerSpools published. Map the selected OrcaSlicer printer to the printer
id in the file under `printers` in the Config tab.

Requires OrcaSlicer 2.5 or later, whose plugin system is still new.
