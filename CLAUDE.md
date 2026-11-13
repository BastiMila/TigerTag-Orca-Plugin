# CLAUDE.md

An OrcaSlicer 2.5 plugin that brings the spools of a TigerTag account into the
slicer. Started 2026-11-13. Nothing is built yet: this file is what is known.

## Goal, in the order it is built

1. **Filament presets from the inventory.** For every spool in the account:
   material, colour, brand, temperatures - written as OrcaSlicer filament
   presets, the way `spoolman2slicer` does it for Spoolman.
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
- Presets: `orca.host` is read-only. A plugin may write preset files into a
  local bundle and reload it - `orca.host.preset.bundles.reload_local(bundle_id)`
  per the release notes; the dev guide spells the module `orca.host.preset_bundle()`.
  Which one is real is unresolved: find out from the API itself.
- No API was found for the printer's filament slots (AMS, "sync filaments").
- There are no example plugins in the Orca repository.

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
- Spools: `users/{uid}/inventory/{chip UID, upper-case hex}`. Fields seen in the
  firmwares: `weight_available` (filament left, grams, written by the scale),
  `measure_gr` / `measure` (full quantity), `container_weight` (empty spool),
  `twin_tag_uid` (second chip of a Bambu spool - two documents, one spool),
  `deleted`, `last_update`, and ids such as `id_material`, `id_brand`. The full
  field list - colour, material name, temperatures - is NOT known yet: read one
  real document with the owner's account first and write the answer here.
- Ids to names (material, brand, ...) come from the public TigerTag database,
  https://raw.githubusercontent.com/TigerTag-Project/TigerTag-RFID-Guide/main/database/
  (TigerSpool mirrors it in `firmware/tools/tigertag_db/`).

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
