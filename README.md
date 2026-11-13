# TigerTag for OrcaSlicer

An OrcaSlicer 2.5 plugin that brings the spools of your
[TigerTag](https://github.com/TigerTag-Project) account into the slicer.

**Status: stage 1 written, not yet tested inside OrcaSlicer.**

1. **Filament presets from your spools** (written): one preset per spool, with
   brand, colour, diameter, density and the nozzle and bed temperature ranges
   read from the spool. Each preset inherits a matching OrcaSlicer system
   preset, such as `SUNLU PLA+ @System` or `Generic PETG @System`.
2. A TigerTag tab listing your spools and what is left on them, with a warning
   when a job needs more than the spool holds (planned).

The plugin only reads your account. It never changes it.

## Install

1. OrcaSlicer: Plugins window, *Install local plugin*, pick `tigertag_orca_plugin.py`.
   Allow network access for the TigerTag and Google hosts when asked.
2. In the plugin's Config tab, enter your TigerTag `email` and `password`.
   The password is used once. Afterwards the plugin keeps only a session
   token, in `.session.json` in its plugin folder.
3. Run *TigerTag: sync filament presets*. Run it again whenever your spools change.

The presets land in the local preset bundle `tigertag`. A sync replaces the
whole bundle, so edits made to these presets are lost on the next sync. Save
a copy under another name to keep one.

## Development

    python -m unittest discover -s tests -v
    python -I tools/dry_run.py LOCAL.md <out dir>   # sync against a real account, outside Orca

Requires OrcaSlicer 2.5 or later, whose plugin system is still new.
