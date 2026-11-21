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
   2026-11-13. Run against a real TigerSpool on 2026-11-14: the FlashForge
   Creator 5 Pro's published slots 1, 2, 4 got their presets (slot 4 the
   owner's saved copy "... Selbst"), the null slot 3 kept its preset.
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
     `printerName` matches `printer_model`, and saves that choice in
     `<data_dir>/tigertag_plugin/printers.json` (shared by all actions; the
     config of each action can override it). Several printers: one entry per
     Orca printer preset; two identical printers need two Orca presets.
   - The tab chooses the entry for the active printer preset (written
     2026-11-14, tested in Orca the same day): automatic (no entry), none
     (`""`), or an account printer. The list comes from `inventory.json`
     (`printers`, written by every sync), so the tab makes no request. A
     Script's config `printers` still overrides it and the tab cannot see it.
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
   Written 2026-11-14 (Pages capability `TigerTagInventory`), unit-tested, not
   yet run in Orca. What shaped it:
   - A page's `on_message` and `on_lifecycle_event` run on the UI thread
     (`PluginPages.cpp`, `PluginManager.cpp`); `post_message` is thread-safe.
     The tab therefore makes no request: every sync writes
     `<data_dir>/tigertag_plugin/inventory.json`, the tab reads it.
   - Config is per capability: the tab cannot read the Script's e-mail or
     `printers`. Everything shared (session, printer mapping, inventory)
     therefore lives in `<data_dir>/tigertag_plugin/`.
   - Sync button in the tab (written and tested in Orca 2026-11-14, slots
     assigned from the thread): the
     click arrives on the UI thread, so the sync runs on its own
     `threading.Thread` (permission `threading=True`, asked at load). It signs
     in with the stored session only (`StoredSession`), never a password.
     Host calls that touch the UI marshal to the UI thread and release the GIL
     while waiting (`run_on_ui_blocking` in `PluginHostUi.cpp`), so the thread
     cannot block Orca. `begin_update` from this thread works like from the
     Script worker.
   - Tested 2026-11-14: tab shows slots, places and the need of a slice
     (252 g on a 58 g spool -> red warning). The need is shown only while the
     G-code's `filament_settings_id` equals the current slot presets.
   - No API gives the filament used by a slice; `SlicingJobComplete` carries
     only the model name. The tab reads `; filament used [g] = 0.76, 0.96,
     ...` (one value per filament slot, near the end) from Orca's temp G-code,
     checked 2026-11-14 on a real slice:
     `%TEMP%/orcaslicer_model/<day>/<time>#<pid>#<n>/Metadata/.<pid>.<plate>.gcode`
     - a hidden name, so `glob(..., include_hidden=True)`.
   - Orca crashed three times on 2026-11-14 (19:45, 20:46, 21:08; dumps in
     `%LOCALAPPDATA%/CrashDumps`, unsymbolized stacks in `log/crash_*.log`).
     Cause not proven; the 20:46 stack contains `PyInit_orca`, the 21:08 crash
     came 5 s after the send action while the printer connected. Since then
     the tab touches no preset data inside a lifecycle event (they fire in the
     middle of preset switches and AMS syncs): `SlicingJobComplete` only posts
     "changed", the page asks back, and it polls every 5 s while visible.
     `PresetSelected` is not handled.
   - A page's `on_message` receives the message already parsed (a dict), not
     JSON text, although the C++ side dumps it to a string.
   - `[AUDIT] block path=...` lines are a pre-check, not a denial: with the
     tree grant the reads succeed and no `AUDIT PROMPT` follows.
   - Every file access outside `data_dir()` raises an audit prompt (the first
     version raised 47: one per scanned folder, and `tempfile.gettempdir()`
     alone raised three by probing Temp with a file). The plugin now asks
     once at load for `fs_read=[<TEMP>/orcaslicer_model/*]` - a "/*" target
     grants a whole tree - and takes the folder from `TEMP`.
   - Places: inventory field `rack` {id, level, position}, 49 of the owner's
     62 spools have one; rack names in `users/{uid}/racks/{id}.name` ("Regal",
     "Schrank Links", ...). Shown as Studio does: "Karton Sunlu 250G, B5".
2b. **Load request, Orca -> TigerSpool.** Action "TigerTag: send slots to
   TigerSpool" and the tab's "Send to TigerSpool" button write the request;
   TigerSpool shows at the printer which spool goes into which slot and where
   it is stored. The tab shows the request before it is sent
   (`load_request_preview`, for the printer chosen in the tab) and marks a slot
   "loaded" when TigerSpool's `slots` holds one of its chips (as of the last
   sync or send, kept in `<data_dir>/tigertag_plugin/slot_maps.json`).
   Written 2026-11-14. Sent from the tab the same evening: both requests
   (Testprinter, FlashForge) landed in the box document, intact; the device
   did not show them yet (firmware side not confirmed on hardware).
   - Where (TigerSpool session, 2026-11-14): not a collection of its own -
     Firestore rules are per collection and `users/{uid}/load_requests` has
     none (it gave HTTP 403; the owner cannot deploy rules to
     `tigertag-connect`). Instead a field of each box document that already
     allows the user: `users/{uid}/tigerspools/{mac}.load_requests.<printerId>`,
     in every box whose `printer_ids` holds the printer - or, since
     `printer_ids` lives in the box's RAM and is empty after a reboot until
     its first account sync (seen 2026-11-15), whose `slots` or
     `load_requests` name it (`box_serves`). Written with one
     `documents:commit`, `updateMask.fieldPaths = ["load_requests.<printerId>"]`
     and `currentDocument.exists`, so a new request replaces only that
     printer's entry. A printer id that is not `[A-Za-z_][A-Za-z_0-9]*` goes
     in backticks - both of the owner's ids start with a digit, so both do.
     The box's heartbeat never puts `load_requests` in its mask. This extends
     the presence document: a prototype to settle with the TigerTag developers,
     like the weight write-back.
   - Format: `{source: "orcaslicer", requested_at: ms, job, slots: {<printer
     slot key>: {uid, uids: [one or both chips], preset, brand, material,
     color, grams_needed, rack: {id, name, level, position} | null, place}}}`.
     Only slots holding a TigerTag spool; after a slice only the slots the
     plate uses. The device reads `uids` (else `uid`), `brand`, `material`,
     `color` (a swatch), `rack` and `requested_at` (ignored after 24 h); not
     `job`, `grams_needed`, `place` (asked for `brand` 2026-11-15). It builds
     the place from `rack` itself (`<name>, B5`; only `B5` if its font cannot
     draw the name).
   - Spools without a chip (`TIGERDATA_...`) stay in the request; the device
     shows "no TigerTag set yet" and does not warn, but can never see them
     loaded. The tab therefore has "Loaded - remove from request" on such a
     slot (written 2026-11-15, unit-tested, not run in Orca): it deletes
     `load_requests.<printerId>.slots.<key>` in every box (a masked field
     missing from `fields` is deleted) and records {printer: {key: uid}} in
     `<data_dir>/tigertag_plugin/confirmed.json`; later sends leave that
     slot out while the same spool is asked for in it.
   - The device reads it with the first full heartbeat after boot, then every
     second heartbeat (about a minute) while the screen is on. A slot is done
     when `slots.<printerId>.<key>.uid` is one of `uids`; the device writes
     nothing back. On the device: requested slots outlined orange, "load
     marked slots (n)"; the scan screen shows the wanted material and place;
     sending another spool still works and says it is not the one asked for.
     Device side built on TigerSpool branch `afc-weight-writeback`, not yet
     on hardware (2026-11-14).
   - `rack.level` and `rack.position` count from 0; Tiger Studio shows the
     level as a letter and the position from 1 (stored level 1, position 4 =
     "B5", confirmed by the owner on 2026-11-14). The plugin shows it the same
     way.
   - Orca 2.5.1 no longer crashed after the tab changes (both came together,
     so which of the two fixed it is unknown).
3. **Later, deliberately not first: subtract what a print used** from the
   spool's `weight_available`. That field belongs to the TigerScale, and a
   second writer on it is an open question with TigerTag (the same question is
   open for TigerSpool, issue #31 in TigerTag-Project/TigerSpool-RFID). Do not
   build it until the owner says so.

1 and 2 only read the account; 2b writes only `load_requests` into the
TigerSpool documents.

**Presets-only edition** (decided 2026-11-15: the owner wants to publish only
stage 1 on Orca Cloud). `tools/build_presets_edition.py` generates
`dist/tigertag_presets.py` ("TigerTag Filament Presets", own version in the
script) from the one source: it drops `# region printer` blocks and uncomments
`# region presets-only` blocks. Everything about slots, printers, the tab and
TigerSpool goes inside a printer region. Tests build it and check that it
holds no printer part and makes the same presets. Not yet run in Orca. It shares
the bundle `tigertag` and `<data_dir>/tigertag_plugin/` with the full plugin. The owner's decision: build first, then present
it to the TigerTag developers - do not ask them beforehand.

## OrcaSlicer 2.5 plugins - what is documented

2.5.0 was released 2026-11-07, 2.5.1 on 2026-11-14; "the plugin system is new
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
  The owner runs v2.5.1 since 2026-11-14 (before: v2.5.0-rc, build af2ffb8c).
  Between the RC and 2.5.1 no commit touched the preset, page or lifecycle
  bindings; new is a Permissions tab in the Plugins window (grants can be
  viewed and edited there).

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
