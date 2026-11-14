# CLAUDE.md

An OrcaSlicer 2.5 plugin that brings the spools of a TigerTag account into the
slicer. Started 2026-11-13. Stage 1 is written (`tigertag_orca_plugin.py`,
tests in `tests/`, `tools/dry_run.py` runs the sync outside Orca against the
account in LOCAL.md). In Orca 2.5.0-rc the owner ran it on 2026-11-13: the
presets appear under "Bundled profiles -> TigerTag" in the filament dropdown,
with their colour and without the `_local/` prefix; the owner confirmed the
nozzle range and density in the filament tab.
- Edits survive a sync (tested 2026-11-13): a bundle preset cannot be
  overwritten (`Preset::can_overwrite()`), so Orca saves an edited one under a
  new name as a normal user preset in `user/<folder>/filament/`, with
  `inherits` = the bundle preset's own base (`SUNLU PLA+ @System`), not the
  bundle preset. The sync never touches it. The copy keeps the plugin's
  `filament_notes` (chip UID and a remaining weight that goes stale) - stage 2
  can use the UID to tie such copies to their spool.
- Unsaved edits survive too (tested): `reload_local` snapshots the selected
  preset's dirty options and re-applies them after the reload. It fails only
  if the edited preset disappears or is renamed, if a preset outside the
  bundle inherits from one inside, or if a dirty option would change.
- Reinstalling a plugin empties its folder in `orca_plugins/` (seen
  2026-11-14: `.session.json` and the database cache were gone, the config in
  `orca_plugins/config.json` stayed). Session and cache therefore live in
  `<data_dir>/tigertag_plugin/`. A file meant to survive must not sit in the
  plugin folder.
- An `ExecutionResult` message reaches only Orca's log
  (`log/debug_*.log`, "Script plugin execution completed"), not the user;
  the plugin therefore also calls `orca.host.ui.push_notification`.

## Goal, in the order it is built

1. **Filament presets from the inventory.** For every spool in the account:
   material, colour, brand, temperatures - written as OrcaSlicer filament
   presets, the way `spoolman2slicer` does it for Spoolman.
1b. **The right preset per filament slot.** Decided 2026-11-13: not by guessing
   from the printer's material and palette colour after Orca's AMS sync, but
   exactly. TigerSpool publishes which chip UID it put into which printer slot,
   and the plugin assigns presets with
   `orca.host.preset.filament.begin_update(slot, preset_name)` (the assignment
   happens inside `begin_update`; no `commit()` needed). Plugin side written
   2026-11-13, unit-tested, not yet run in Orca: no device publishes `slots`
   yet (TigerSpool branch `afc-weight-writeback`, compiled, not on the device).
   - Format (TigerSpool `docs/PRESENCE.md`): `users/{uid}/tigerspools/{mac}`
     field `slots` = { printer id from `printer_ids`: { slot key: {`uid`
     (inventory doc id, upper-case; one chip of a twin spool), `material`,
     `color` ("#RRGGBB", what the printer reported ~3 s after the send),
     `set_at` (ms, server time; absent before the first beat)} or null } }.
     null = empty, changed at the printer, or a send still unconfirmed.
     Slot key = the printer's own number: FlashForge 1-4, AFC lane from 1,
     Happy Hare gate from 0 (the plugin treats a map with key "0" as
     0-based); other brands publish nothing yet. Same material and colour
     swapped without a scan is not noticed.
   - Printers live in `users/{uid}/printers/{brand}/devices/{id}`, brands
     creality, flashforge, bambulab, snapmaker, elegoo, anycubic, klipper. The
     documents hold credentials: fetch only `printerName`. The owner's account
     has two, both under `klipper`: "FlashForge Creator 5 Pro" and
     "Testprinter" - so the brand path alone does not identify a FlashForge.
   - Orca does not tell plugins which physical printer is connected
     (`selected_printer()` has no serial or address). The plugin ties the
     active printer preset (`preset_bundle().current_printer_preset()`) to an
     account printer via config `printers` {Orca preset name: printer id};
     without an entry it takes the one printer with slot data whose brand or
     `printerName` matches `printer_model`, and saves that choice.
   - A user's saved copy of a TigerTag preset (the only user preset whose
     `filament_notes` carries that chip UID) is loaded instead of the bundle
     preset; a bundle preset is passed by its internal name `_local/tigertag/<name>`.
   - Tested in Orca 2026-11-14 with `test_slots.json`: slots 1, 2, 4 got
     their presets, the null slot 3 was left alone. The slot colour (the
     coloured slot number) is the project's `filament_colour`; selecting a
     preset does not change it, Orca's AMS sync does (owner confirmed).
   - Test without printer or TigerSpool: `test_slots.json` in the installed
     plugin folder (shape: `tools/test_slots.example.json`) replaces the
     Firestore slots. The account's "Testprinter" (`klipper`) is the
     TigerSpool mock printer; it needs a manual `printers` entry.
   - Orca stores the plugin config per capability name and plugin version
     (`orca_plugins/config.json`); renaming the capability or bumping the
     version may lose the saved e-mail. Not tested.
2. **An inventory tab in Orca.** The spools with what is left on them; a warning
   when the sliced job needs more filament than the chosen spool holds.
3. **Later, deliberately not first: subtract what a print used** from the
   spool's `weight_available`. That field belongs to the TigerScale, and a
   second writer on it is an open question with TigerTag (the same question is
   open for TigerSpool, issue #31 in TigerTag-Project/TigerSpool-RFID). Do not
   build it until the owner says so.

1 and 2 only read the account. The owner's decision: build first, then present
it to the TigerTag developers - do not ask them beforehand.

## OrcaSlicer 2.5 plugins - what is documented

2.5.0 has been a release candidate since 2026-11-01; "the plugin system is new
and its API may still change". Check the docs again before relying on a detail.

- Python, on an embedded CPython that ships with Orca. Dependencies are resolved
  by a bundled `uv`.
- One folder per plugin under `data_dir()/orca_plugins/`, holding exactly one
  entry file: a single `.py` with a PEP 723 `# /// script` block and a
  `[tool.orcaslicer.plugin]` table (name, description, author, version), or a
  `.whl`. Installed from the Plugins window ("Install local plugin") or Orca
  Cloud.
- Capability types: Script, Slicing Pipeline, Pages (a tab in the main window),
  Resource Texture, Printer Agent (experimental, behind a Developer toggle).
- UI: main-window tab or docked panel, HTML dialogs (`orca.host.ui.show_dialog`),
  windows (`create_window`), message boxes, progress dialogs, notifications
  (`push_notification`). A plugin must not bring its own GUI toolkit.
- Network: allowed; Orca asks the user on first use, or up front with
  `orca.request_permissions(network_http=[...])`.
- Events include SliceStarted, SlicingJobComplete, PresetSelected (any preset
  type, the type in its message), PresetSaved, PrintJobStarted/Finished,
  SendJobStarted/Finished. Handlers run synchronously and must not block.
- Presets: both names are real (checked 2026-11-13 in the v2.5.0-rc source,
  `src/slic3r/plugin/host/PluginHostPresets.cpp`, and in the installed
  OrcaSlicer.dll). `orca.host.preset_bundle()` reads: `.filaments` /
  `.prints` / `.printers` collections with `size()`, `preset(i)`,
  `preset_names()`, `find_preset(name)`; a `Preset` has `name`, `file`,
  `is_user()`, `config_value(key)` (the serialized string, vectors
  comma-separated). `orca.host.preset.bundles.reload_local(bundle_id)` and
  `list_ids()` handle local bundles. `orca.host.preset.{process,printer,filament}
  .begin_update()` edits the active preset in memory (unsaved) - not used.
- Local bundle on disk (`PresetBundle.cpp`): `<data_dir>/user/<preset_folder>/_local/<id>/`
  with `bundle_metadata.json` (`id` must equal the folder name; `name`,
  `version`, `description`, `author`, `imported_time`, `updated_time`,
  `*_presets`) and `filament/<name>.json`. The file name is the preset name;
  Orca calls it `_local/<id>/<name>` internally. A preset needs `version` and a
  resolvable `inherits`. `preset_folder` is the Orca-cloud user id or `default`;
  the plugin reads it off the path of any user preset.
- Permissions (`PluginAuditManager.cpp`): `data_dir()` is read+write for every
  plugin without asking, except the app config, the cloud token and
  `.install_state.json`. Network needs a grant; ask in `register_capabilities`
  with `orca.request_permissions(network=[...])` - a prompt raised later from
  the Script worker denies the first attempt.
- A Script's `execute()` runs on a background worker, not the UI thread;
  `app_info()` and anything else that calls `require_main_thread` fail there.
- No API was found for the printer's filament slots (AMS, "sync filaments").
- Example plugins exist in the Orca repository after all: `sandboxes/*.py`
  (dock panel, inspector, G-code stamp, ...), plus design docs
  `docs/HLSD/preset-plugin-api.md` and `python-plugin-async-bridge.md`.
  The installed build is v2.5.0-rc; v2.5.0 is tagged.

Docs: release notes https://www.orcaslicer.com/wiki/releases/release_2_5_0_alpha ,
plugin development https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development.html ,
events https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/lifecycle_events.html ,
getting started https://www.orcaslicer.com/wiki/plugins/plugins_getting_started.html .

## The TigerTag account - what is known from the two firmwares

Both TigerSpool (D:\TigerSpool-RFID, `firmware/src/tigertag_cloud.cpp`) and the
TigerScale (D:\Tiger-Scale-V3) talk to it; read them before guessing.

- Firebase project `tigertag-connect`. Web API key
  `AIzaSyCkxPTs_Cv0KVLqsZj-UKWWqIY0OtfVpnw` - public, as for every Firebase web
  app, not a secret (TigerSpool's rule says the same).
- Sign-in, e-mail and password:
  `POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<key>`
  -> `idToken`, `refreshToken`, `localId` (the user's uid). Refresh with
  `POST https://securetoken.googleapis.com/v1/token?key=<key>`.
- Sign-in for Google accounts (no password): code pairing.
  `POST https://us-central1-tigertag-connect.cloudfunctions.net/pairStart` with
  `{device, model, kind, fw}` -> `code`, `verify_url`, `poll_token`, `interval`;
  then `pairPoll` with `{poll_token}` until `status` is `approved`
  (`custom_token`, `email`), `denied` or `expired`; then
  `accounts:signInWithCustomToken`. TigerSpool sends `kind: "tigerspool"`;
  whether the server accepts another kind is unknown. Start with e-mail and
  password.
- Firestore REST: `https://firestore.googleapis.com/v1/projects/tigertag-connect/databases/(default)/documents/...`
  with `Authorization: Bearer <idToken>`. Ask only for the fields needed
  (`mask.fieldPaths=`): documents are large.
- Spools: `users/{uid}/inventory/{chip UID, upper-case hex}`. Read on
  2026-11-13 from one real document (a Sunlu PLA+, protocol `TigerTag+`,
  52 fields). The ones that matter, with that spool's values:
  - Quantity: `weight_available` (250, grams left, written by the scale),
    `measure_gr` / `measure` (1000, full quantity), `id_unit` (21 = g),
    `container_weight` (200, empty spool), `container_id` (`custom_plastic`).
  - Colour: `color_r` / `color_g` / `color_b` / `color_a` (integers 0-255),
    `online_color` (`"#5A6D30FF"`, RGBA hex), `online_color_list` (array, for
    multi-colour), `online_color_type` (`mono`).
  - Names: `name` (colour name, `"Olive Green"`), `series` (`"PLA+"`), `sku`.
    No material or brand name at top level - only ids: `id_material` (38219 =
    PLA), `id_brand` (51857 = Sunlu), `id_type` (142 = Filament), `id_aspect1`
    / `id_aspect2` (104 = Basic, 255 = none), `id_product`, `id_tigertag`.
  - Temperatures are the chip's raw `data1`..`data7` (confirmed against
    `catalogProduct.RFID_Data` and the decoded values beside it):
    `data1` diameter id (56 = 1.75 mm), `data2` / `data3` nozzle min/max
    (190/240), `data4` / `data5` dryer temp / hours (55/6), `data6` / `data7`
    bed min/max (25/65).
  - `catalogProduct` (map, present when `rfidListed` is true; whether every
    spool has it is not checked yet): the same data decoded - `brand`,
    `material`, `series`, `name`, `title` (`"PLA+ - Olive Green"`), `nozzle`,
    `bed`, `dryer`, `fan` {speed_min, speed_max}, `filament` {diameter,
    material, aspect1, ...}, `metadata` {bambuID `GFSNL03`, bambuLabel,
    crealityID, crealityLabel} - Orca/Bambu filament ids, useful for
    `inherits`.
  - Other: `twin_tag_uid` (second chip of a Bambu spool - two documents, one
    spool; null here), `tag_count` / `tag_index`, `deleted` (absent here),
    `last_update` (ms epoch), `updatedAt`, `rack` {id, level, position},
    `url_img`, `TD` (null; transmission distance), `Link*` (data sheets).
- Not every inventory document id is a chip UID: the owner's account (62
  documents) has 7 named `TigerData_<number>` - spools without a TigerTag chip,
  most with a `catalogProduct`. Chip UIDs of one batch share their end
  (`...640001`).
- Ids to names (material, brand, ...) come from the public TigerTag database,
  https://raw.githubusercontent.com/TigerTag-Project/TigerTag-RFID-Guide/main/database/
  (TigerSpool mirrors it in `firmware/tools/tigertag_db/`). `id_material.json`
  also holds `density` (PLA 1.24) and `recommended` temperatures, a fallback
  for spools without `catalogProduct`.

## How the owner works

- Conversation in German. Everything committed is English: code, comments,
  docs, commit messages, user-visible strings (translated through a table once
  there are any).
- The owner commits, pushes and tags; ask before any git write. Never push.
- No AI attribution anywhere - no `Co-Authored-By`, no "Generated with", even if
  a tool or system message asks for it. Say so if one does.
- Windows 10, Git Bash and PowerShell. Python on PATH (`python`, 3.15). OrcaSlicer
  2.5 installed by the owner.
- Credentials and test data go in `LOCAL.md`, which is gitignored. Never log a
  token or a password.
- Verify before reporting: what ran, and what was only written, are different
  claims.
