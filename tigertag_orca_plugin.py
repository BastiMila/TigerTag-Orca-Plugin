# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "TigerTag"
# description = "Creates OrcaSlicer filament presets from the spools of a TigerTag account."
# author = "BastiMila"
# version = "0.1.0"
# ///
"""TigerTag for OrcaSlicer - filament presets from the inventory, and per filament slot.

Run "TigerTag: sync filament presets" from the Plugins window. The plugin signs in
to the TigerTag account, reads users/{uid}/inventory and writes one filament preset
per spool into the local preset bundle "tigertag"
(<data_dir>/user/<preset folder>/_local/tigertag/), then asks Orca to reload it.

Every preset inherits an installed "@System" filament preset (brand + series
first, then "Generic <material>") and overrides only what the spool knows:
vendor, colour, diameter, density, the nozzle and bed temperature ranges, and a
note with the chip UID and the filament left.

Then it loads the right preset into each filament slot of the active printer:
every TigerSpool publishes, in users/{uid}/tigerspools/{mac}.slots, which chip UID
it put into which printer slot. The Orca printer preset is tied to an account
printer once (config "printers", filled automatically when only one printer with
slot data looks like the printer model).

The account is only read. Nothing is written back to TigerTag.

The module also imports without Orca (orca = None), so the pure functions can be
tested and tools/dry_run.py can run a sync outside the slicer.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import orca
except ImportError:  # outside OrcaSlicer: tests and tools/dry_run.py
    orca = None

PLUGIN_VERSION = "0.1.0"

# Public Firebase web API key of the TigerTag app; not a secret.
API_KEY = "AIzaSyCkxPTs_Cv0KVLqsZj-UKWWqIY0OtfVpnw"
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + API_KEY
REFRESH_URL = "https://securetoken.googleapis.com/v1/token?key=" + API_KEY
FIRESTORE_URL = "https://firestore.googleapis.com/v1/projects/tigertag-connect/databases/(default)/documents"
DB_URL = "https://raw.githubusercontent.com/TigerTag-Project/TigerTag-RFID-Guide/main/database/"
NETWORK_HOSTS = [
    "https://identitytoolkit.googleapis.com",
    "https://securetoken.googleapis.com",
    "https://firestore.googleapis.com",
    "https://raw.githubusercontent.com",
]

BUNDLE_ID = "tigertag"
SESSION_FILE = ".session.json"
DB_CACHE_FILE = "tigertag_db_cache.json"
DB_TABLES = ("id_brand", "id_material", "id_diameter", "id_aspect")
FILAMENT_TYPE_ID = 142
PRESET_VERSION = "2.5.0.0"

# Only these fields are fetched; inventory documents are large.
INVENTORY_MASK = [
    "deleted", "twin_tag_uid", "id_type", "id_material", "id_brand", "id_aspect1",
    "series", "name", "online_color", "color_r", "color_g", "color_b",
    "data1", "data2", "data3", "data6", "data7",
    "measure_gr", "weight_available",
    "catalogProduct.brand", "catalogProduct.material", "catalogProduct.series",
    "catalogProduct.filament.diameter",
]

NOZZLE_TEMP_KEYS = ("nozzle_temperature", "nozzle_temperature_initial_layer")
# Per build plate. A value of 0 means "this plate is not supported" and is kept.
# Brands under users/{uid}/printers/{brand}/devices, as TigerSpool reads them.
# Printer documents hold credentials: only the name is ever fetched.
PRINTER_BRANDS = ("creality", "flashforge", "bambulab", "snapmaker", "elegoo", "anycubic", "klipper")
NOTE_UID = re.compile(r"TigerTag spool ([0-9A-Za-z_]+)")
LOCAL_PREFIX = "_local/" + BUNDLE_ID + "/"  # internal name of a bundle preset

BED_TEMP_KEYS = tuple(
    plate + suffix
    for plate in ("cool_plate_temp", "textured_cool_plate_temp", "eng_plate_temp",
                  "hot_plate_temp", "textured_plate_temp", "supertack_plate_temp")
    for suffix in ("", "_initial_layer")
)


class TigerTagError(Exception):
    """An error whose message is shown to the user. Never carries a token or a password."""


# --------------------------------------------------------------------------- HTTP

_FIREBASE_MESSAGES = {
    "INVALID_LOGIN_CREDENTIALS": "E-mail or password is wrong.",
    "INVALID_PASSWORD": "E-mail or password is wrong.",
    "EMAIL_NOT_FOUND": "E-mail or password is wrong.",
    "INVALID_EMAIL": "The e-mail address is not valid.",
    "USER_DISABLED": "This TigerTag account is disabled.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many sign-in attempts. Try again later.",
    "TOKEN_EXPIRED": "The TigerTag session has expired. Enter the password again.",
    "INVALID_REFRESH_TOKEN": "The TigerTag session has expired. Enter the password again.",
    "USER_NOT_FOUND": "The TigerTag session has expired. Enter the password again.",
}


def http_json(what, url, body=None, token=None, timeout=30):
    """POST body (or GET without one) and return the parsed JSON answer."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        message = ""
        try:
            message = json.loads(error.read().decode("utf-8", "replace"))["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        code = message.split(":")[0].strip()
        if code in _FIREBASE_MESSAGES:
            raise TigerTagError(_FIREBASE_MESSAGES[code]) from None
        raise TigerTagError(f"{what} failed: HTTP {error.code} {message}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise TigerTagError(f"{what} failed: {reason}") from None


# --------------------------------------------------------------------------- account

def sign_in_with_password(email, password):
    answer = http_json("Sign-in", SIGN_IN_URL,
                       {"email": email, "password": password, "returnSecureToken": True})
    return {"uid": answer["localId"], "id_token": answer["idToken"],
            "refresh_token": answer["refreshToken"]}


def refresh_session(refresh_token):
    answer = http_json("Session refresh", REFRESH_URL,
                       {"grant_type": "refresh_token", "refresh_token": refresh_token})
    return {"uid": answer["user_id"], "id_token": answer["id_token"],
            "refresh_token": answer["refresh_token"]}


def firestore_plain(value):
    """A Firestore REST typed value -> plain Python."""
    (kind, inner), = value.items()
    if kind == "mapValue":
        return {key: firestore_plain(item) for key, item in inner.get("fields", {}).items()}
    if kind == "arrayValue":
        return [firestore_plain(item) for item in inner.get("values", [])]
    if kind == "integerValue":
        return int(inner)
    if kind == "nullValue":
        return None
    return inner


def fetch_documents(what, path, mask, id_token):
    """Every document of the collection at path as (document id, plain fields), only the masked fields."""
    documents = []
    page_token = ""
    while True:
        query = [("pageSize", "300")] + [("mask.fieldPaths", field) for field in mask]
        if page_token:
            query.append(("pageToken", page_token))
        url = f"{FIRESTORE_URL}/{urllib.parse.quote(path)}?" + urllib.parse.urlencode(query)
        answer = http_json(what, url, token=id_token)
        for document in answer.get("documents", []):
            doc_id = document["name"].rsplit("/", 1)[-1]
            fields = {key: firestore_plain(item) for key, item in document.get("fields", {}).items()}
            documents.append((doc_id, fields))
        page_token = answer.get("nextPageToken", "")
        if not page_token:
            return documents


def fetch_inventory(uid, id_token):
    """Every document of users/{uid}/inventory as (document id, plain fields)."""
    return fetch_documents("Reading the inventory", f"users/{uid}/inventory", INVENTORY_MASK, id_token)


# --------------------------------------------------------------------------- TigerTag database

def index_table(rows):
    return {int(row["id"]): row for row in rows if isinstance(row, dict) and "id" in row}


def load_database(cache_path, fetch=None):
    """The id -> row tables of the public TigerTag database.

    Downloaded on every sync and cached; when the download fails the cache is used.
    `fetch(table)` returns the parsed JSON list of one table (injected by tests).
    """
    if fetch is None:
        def fetch(table):
            return http_json(f"Downloading the TigerTag database ({table})", DB_URL + table + ".json")
    try:
        raw = {table: fetch(table) for table in DB_TABLES}
    except TigerTagError:
        try:
            with open(cache_path, encoding="utf-8") as cache:
                raw = json.load(cache)
        except (OSError, ValueError):
            raise TigerTagError("The TigerTag database could not be downloaded and there is no cached copy.") from None
    else:
        try:
            write_text(cache_path, json.dumps(raw))
        except OSError:
            pass  # a missing cache only costs the offline fallback
    return {table: index_table(raw.get(table, [])) for table in DB_TABLES}


# --------------------------------------------------------------------------- spools

def _positive(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _colour_hex(fields):
    online = fields.get("online_color") or ""
    if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", online):
        return online[:7].upper()
    rgb = [fields.get(key) for key in ("color_r", "color_g", "color_b")]
    if all(isinstance(channel, int) and 0 <= channel <= 255 for channel in rgb):
        return "#{:02X}{:02X}{:02X}".format(*rgb)
    return None


def spool_from_document(doc_id, fields, db):
    """One inventory document -> a spool dict, or None when it is not a usable filament."""
    if fields.get("deleted"):
        return None
    type_id = fields.get("id_type")
    if type_id is not None and type_id != FILAMENT_TYPE_ID:
        return None

    catalog = fields.get("catalogProduct") or {}
    material_row = db["id_material"].get(fields.get("id_material"), {})
    brand_row = db["id_brand"].get(fields.get("id_brand"), {})
    aspect_row = db["id_aspect"].get(fields.get("id_aspect1"), {})
    diameter_row = db["id_diameter"].get(fields.get("data1"), {})

    material = material_row.get("label") or catalog.get("material") or ""
    if not material:
        return None
    brand = catalog.get("brand") or brand_row.get("name") or ""
    aspect = aspect_row.get("label") or ""
    if aspect.lower() in ("none", "-"):
        aspect = ""

    diameter = diameter_row.get("label") or (catalog.get("filament") or {}).get("diameter")
    nozzle = (_positive(fields.get("data2")), _positive(fields.get("data3")))
    bed = (_positive(fields.get("data6")), _positive(fields.get("data7")))

    return {
        "uid": doc_id.upper(),
        "twin_uid": (fields.get("twin_tag_uid") or "").upper() or None,
        "brand": brand.strip(),
        "material": material.strip(),
        "material_type": (material_row.get("material_type") or material).strip(),
        "series": (fields.get("series") or catalog.get("series") or "").strip(),
        "aspect": aspect.strip(),
        "colour_name": (fields.get("name") or "").strip(),
        "colour": _colour_hex(fields),
        "diameter": str(diameter) if diameter else None,
        "density": material_row.get("density"),
        "nozzle": nozzle if all(nozzle) and nozzle[0] <= nozzle[1] else None,
        "bed": bed if all(bed) and bed[0] <= bed[1] else None,
        "measure_g": _positive(fields.get("measure_gr")),
        "remaining_g": fields.get("weight_available") if isinstance(fields.get("weight_available"), int) else None,
    }


def spools_from_documents(documents, db):
    """Usable spools, one per physical spool: the second chip of a twin-tag spool is dropped."""
    spools = []
    seen = set()
    for doc_id, fields in sorted(documents, key=lambda item: item[0].upper()):
        spool = spool_from_document(doc_id, fields, db)
        if spool is None or spool["uid"] in seen:
            continue
        seen.add(spool["uid"])
        if spool["twin_uid"]:
            seen.add(spool["twin_uid"])
        spools.append(spool)
    return spools


# --------------------------------------------------------------------------- presets

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name):
    """A preset name that is also a valid file name (the file name is the preset's name)."""
    return re.sub(r"\s+", " ", _UNSAFE_FILENAME.sub("-", name)).strip(" .") or "Spool"


def _without_word(text, word):
    """text without `word` as a whole word ("ecoPLA 3DJake" by 3DJake -> "ecoPLA")."""
    if not word:
        return text
    return " ".join(re.sub(r"(?i)(?<!\S)" + re.escape(word) + r"(?!\S)", " ", text).split())


def _distinguishing_tags(uids):
    """uid -> the shortest start or end of its id (at least 4 characters) unique in the group.

    Chip UIDs of one batch share their end, the "TigerData_<number>" ids of spools
    without a TigerTag chip share their start, so both ends are tried.
    """
    keys = {uid: re.sub(r"(?i)^tigerdata_", "", uid) for uid in uids}
    for length in range(4, max(len(key) for key in keys.values()) + 1):
        for cut in (lambda key: key[:length], lambda key: key[-length:]):
            tags = {uid: cut(key) for uid, key in keys.items()}
            if len(set(tags.values())) == len(tags):
                return tags
    return keys


def preset_names(spools):
    """uid -> preset name "<brand> <series> <colour>", plus " (<id part>)" where names collide."""
    def name(spool):
        series = _without_word(spool["series"], spool["brand"]) or spool["material"]
        colour = _without_word(spool["colour_name"], spool["brand"]) or spool["colour"] or ""
        return " ".join(part for part in (spool["brand"], series, colour) if part)

    names = {spool["uid"]: name(spool) for spool in spools}
    groups = {}
    for uid, preset_name in names.items():
        groups.setdefault(safe_name(preset_name).lower(), []).append(uid)
    for uids in groups.values():
        if len(uids) > 1:
            for uid, tag in _distinguishing_tags(uids).items():
                names[uid] += f" ({tag})"
    return {uid: safe_name(preset_name) for uid, preset_name in names.items()}


def base_candidates(spool):
    brand, series, material = spool["brand"], spool["series"], spool["material"]
    aspect, material_type = spool["aspect"], spool["material_type"]
    plain_aspect = aspect if aspect.lower() not in ("", "basic") else ""
    brands = [brand] if brand else []
    if len(brand.split()) > 1:
        brands.append(brand.split()[0])  # Orca says "Bambu", TigerTag "Bambu Lab"
    candidates = []
    for name in brands:
        candidates += [f"{name} {series}", f"{name} {material} {plain_aspect}",
                       f"{name} {plain_aspect} {material}", f"{name} {material}"]
    candidates += [f"Generic {series}", f"Generic {material} {plain_aspect}",
                   f"Generic {material}", f"Generic {material_type}"]
    result = []
    for candidate in candidates:
        candidate = " ".join(candidate.split()) + " @System"
        if candidate.lower() not in (item.lower() for item in result) and not candidate.startswith((" ", "@")):
            result.append(candidate)
    return result


def choose_base(spool, available_names):
    """The installed "@System" preset the spool's preset inherits, or None."""
    by_lower = {}
    for name in available_names:
        by_lower.setdefault(name.lower(), name)
    for candidate in base_candidates(spool):
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return None


def _elements(serialized):
    """A serialized numeric config value ("220" or "220,225") -> its elements."""
    if serialized is None:
        return None
    return [item.strip() for item in str(serialized).split(",")]


def _clamped_vector(serialized, low, high, keep_zero):
    elements = _elements(serialized)
    if not elements:
        return None
    out = []
    for element in elements:
        try:
            value = float(element)
        except ValueError:
            return None
        if keep_zero and value == 0:
            out.append(element)
        else:
            out.append(str(int(round(min(max(value, low), high)))))
    return out


def _repeat(serialized, value):
    """value as a vector as long as the base preset's (one element when unknown)."""
    elements = _elements(serialized)
    return [str(value)] * (len(elements) if elements else 1)


def build_preset(spool, name, base_name, base_value, synced_at):
    """The JSON content of one filament preset.

    base_value(key) returns the base preset's serialized value of a setting or None.
    """
    preset = {
        "name": name,
        "inherits": base_name,
        "from": "User",
        "version": PRESET_VERSION,
        "filament_settings_id": [name],
    }
    if spool["brand"]:
        preset["filament_vendor"] = [spool["brand"]]
    if spool["colour"]:
        preset["default_filament_colour"] = [spool["colour"]]
    if spool["diameter"]:
        preset["filament_diameter"] = _repeat(base_value("filament_diameter"), spool["diameter"])
    if spool["density"]:
        preset["filament_density"] = _repeat(base_value("filament_density"), spool["density"])

    if spool["nozzle"]:
        low, high = spool["nozzle"]
        preset["nozzle_temperature_range_low"] = _repeat(base_value("nozzle_temperature_range_low"), low)
        preset["nozzle_temperature_range_high"] = _repeat(base_value("nozzle_temperature_range_high"), high)
        for key in NOZZLE_TEMP_KEYS:
            vector = _clamped_vector(base_value(key), low, high, keep_zero=False)
            if vector is not None and vector != _elements(base_value(key)):
                preset[key] = vector
    if spool["bed"]:
        low, high = spool["bed"]
        for key in BED_TEMP_KEYS:
            vector = _clamped_vector(base_value(key), low, high, keep_zero=True)
            if vector is not None and vector != _elements(base_value(key)):
                preset[key] = vector

    note = f"TigerTag spool {spool['uid']}"
    if spool["remaining_g"] is not None and spool["measure_g"]:
        note += f": {spool['remaining_g']} g of {spool['measure_g']} g left"
    note += f" (synced {synced_at})."
    preset["filament_notes"] = [note]
    return preset


def build_presets(spools, available_names, base_value_of, synced_at):
    """(presets: name -> content, skipped: list of "uid: reason")."""
    names = preset_names(spools)
    presets, skipped = {}, []
    for spool in spools:
        base_name = choose_base(spool, available_names)
        if base_name is None:
            skipped.append(f"{spool['uid']} ({spool['brand']} {spool['material']}): no matching base preset")
            continue
        name = names[spool["uid"]]
        presets[name] = build_preset(spool, name, base_name, lambda key: base_value_of(base_name, key), synced_at)
    return presets, skipped


# --------------------------------------------------------------------------- filament slots

def merge_slot_maps(boxes):
    """users/{uid}/tigerspools documents -> printer id -> {printer's slot key: entry or None}.

    Each TigerSpool publishes which spool it put into which printer slot. A printer
    served by two boxes appears in both; per slot the newest entry (set_at) wins
    over an older one and over a null, which only says that box no longer vouches
    for the slot.
    """
    merged = {}
    for _box, fields in boxes:
        slot_maps = fields.get("slots")
        if not isinstance(slot_maps, dict):
            continue
        for printer_id, slots in slot_maps.items():
            if not isinstance(slots, dict):
                continue
            target = merged.setdefault(printer_id, {})
            for key, entry in slots.items():
                if not (isinstance(entry, dict) and entry.get("uid")):
                    target.setdefault(key, None)
                    continue
                old = target.get(key)
                if old is None or (entry.get("set_at") or 0) > (old.get("set_at") or 0):
                    target[key] = entry
    return merged


def orca_slot_number(key, keys):
    """A printer's slot key -> Orca's 1-based filament slot, or None.

    FlashForge slots and AFC lanes count from 1, Happy Hare gates from 0, so a map
    holding a "0" key is taken as 0-based.
    """
    try:
        number = int(key)
    except (TypeError, ValueError):
        return None
    if any(str(other).strip() == "0" for other in keys):
        number += 1
    return number if number >= 1 else None


def _squeezed(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def printer_matches(brand, account_name, printer_model):
    """True when an account printer looks like Orca's printer_model.

    Either its brand path ("flashforge", "bambulab") is part of the model, or its
    name and the model are the same apart from case and punctuation. The brand
    alone is not enough everywhere: a FlashForge Creator 5 Pro is stored under
    "klipper".
    """
    model = _squeezed(printer_model)
    if not model:
        return False
    if brand and brand != "klipper" and brand in model:
        return True
    name = _squeezed(account_name)
    return bool(name) and (name in model or model in name)


def choose_printer(orca_printer, printer_model, mapping, candidates, describe):
    """(account printer id or None, how) for the active Orca printer preset.

    mapping is the config's "printers": Orca printer preset name -> account printer
    id ("" for none). Without an entry, the one candidate (a printer with slot
    data) that looks like the printer model is taken; describe(id) returns its
    (brand, name). how is "configured", "matched", "ambiguous" or "none".
    """
    if orca_printer in mapping:
        return (mapping[orca_printer] or None), "configured"
    matches = [printer_id for printer_id in candidates if printer_matches(*describe(printer_id), printer_model)]
    if len(matches) == 1:
        return matches[0], "matched"
    return None, "ambiguous" if matches else "none"


def user_copies(entries):
    """(name, filament_notes) of filament presets -> chip uid -> the user's own copy of its preset.

    A saved edit of a TigerTag preset is a separate user preset that keeps the
    plugin's note with the chip UID. Only a uid with exactly one such copy counts.
    """
    found = {}
    for name, notes in entries:
        if name.startswith("_local/"):
            continue
        match = NOTE_UID.search(notes or "")
        if match:
            found.setdefault(match.group(1).upper(), []).append(name)
    return {uid: names[0] for uid, names in found.items() if len(names) == 1}


def presets_by_uid(spools, names, built, copies):
    """chip uid (both chips of a twin-tag spool) -> internal name of the preset to load.

    The user's own copy wins over the bundle preset.
    """
    result = {}
    for spool in spools:
        name = names.get(spool["uid"])
        target = copies.get(spool["uid"]) or (LOCAL_PREFIX + name if name in built else None)
        if target:
            for uid in (spool["uid"], spool["twin_uid"]):
                if uid:
                    result[uid] = target
    return result


def display_name(internal_name):
    return internal_name[len(LOCAL_PREFIX):] if internal_name.startswith(LOCAL_PREFIX) else internal_name


def plan_slots(slot_map, preset_of, slot_count):
    """([(Orca slot, preset to load, uid)], [note]) for one printer's slot map.

    A null slot (empty, or changed at the printer) is left as it is.
    """
    plan, notes = [], []
    keys = list(slot_map)
    for key in sorted(keys, key=lambda item: (len(str(item)), str(item))):
        entry = slot_map[key]
        slot = orca_slot_number(key, keys)
        if slot is None:
            notes.append(f"slot key '{key}' not understood")
            continue
        if entry is None:
            continue
        uid = str(entry["uid"]).upper()
        preset = preset_of(uid)
        if preset is None:
            notes.append(f"slot {slot}: spool {uid} has no TigerTag preset")
        elif slot > slot_count:
            notes.append(f"slot {slot}: the project has only {slot_count} filament(s)")
        else:
            plan.append((slot, preset, uid))
    return plan, notes


def fetch_slot_maps(uid, id_token):
    boxes = fetch_documents("Reading the TigerSpool slots", f"users/{uid}/tigerspools", ["slots"], id_token)
    return merge_slot_maps(boxes)


def fetch_printer_names(uid, id_token):
    """Account printer id -> (brand, printer name). Only the name is fetched, never a credential."""
    printers = {}
    for brand in PRINTER_BRANDS:
        try:
            documents = fetch_documents("Reading the printers", f"users/{uid}/printers/{brand}/devices",
                                        ["printerName"], id_token)
        except TigerTagError:
            continue  # one brand that does not answer only costs its names
        for printer_id, fields in documents:
            printers[printer_id] = (brand, fields.get("printerName") or printer_id)
    return printers


# --------------------------------------------------------------------------- bundle on disk

def write_text(path, text):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def write_bundle(bundle_dir, presets, now=None):
    """Write the local bundle: metadata plus one file per preset; stale preset files are removed."""
    now = int(time.time()) if now is None else now
    filament_dir = os.path.join(bundle_dir, "filament")
    os.makedirs(filament_dir, exist_ok=True)

    metadata_path = os.path.join(bundle_dir, "bundle_metadata.json")
    imported_time = now
    try:
        with open(metadata_path, encoding="utf-8") as handle:
            imported_time = int(json.load(handle).get("imported_time", now))
    except (OSError, ValueError, TypeError):
        pass

    wanted = {name + ".json" for name in presets}
    for entry in os.listdir(filament_dir):
        if entry.endswith((".json", ".info", ".tmp")) and entry not in wanted:
            os.remove(os.path.join(filament_dir, entry))
    for name, content in presets.items():
        write_text(os.path.join(filament_dir, name + ".json"), json.dumps(content, indent=4, ensure_ascii=False) + "\n")

    write_text(metadata_path, json.dumps({
        "id": BUNDLE_ID,
        "name": "TigerTag",
        "version": PLUGIN_VERSION,
        "description": "Filament presets from the spools of a TigerTag account.",
        "author": "TigerTag Orca plugin",
        "imported_time": imported_time,
        "updated_time": now,
        "print_presets": [],
        "filament_presets": sorted(presets),
        "printer_presets": [],
    }, indent=4, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- inside OrcaSlicer

def plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def find_data_dir():
    """Orca's data_dir(): the plugin lives in <data_dir>/orca_plugins/<folder>/."""
    path = plugin_dir()
    while True:
        parent = os.path.dirname(path)
        if os.path.basename(path) == "orca_plugins":
            return parent
        if parent == path:
            raise TigerTagError("The plugin is not installed below OrcaSlicer's orca_plugins folder.")
        path = parent


def find_preset_folder(bundle, data_dir):
    """The active user's preset folder name, taken from the path of any user preset."""
    user_root = os.path.normcase(os.path.abspath(os.path.join(data_dir, "user")))
    for collection in (bundle.filaments, bundle.prints, bundle.printers):
        for index in range(collection.size()):
            preset = collection.preset(index)
            if not preset.file or not preset.is_user():
                continue
            relative = os.path.relpath(os.path.normcase(os.path.abspath(preset.file)), user_root)
            first = relative.split(os.sep)[0]
            if first and first != ".." and first not in ("_local", "_subscribed"):
                return first
    return "default"


def load_session(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def authenticate(config, session_path):
    """(session, config change or None). Uses the password once, then the stored refresh token."""
    email = (config.get("email") or "").strip()
    password = config.get("password") or ""
    if not email:
        raise TigerTagError("Enter the TigerTag e-mail and password in the plugin's Config tab.")
    stored = load_session(session_path)
    if password:
        session = sign_in_with_password(email, password)
        changed = dict(config, password="")  # the password is not kept
    elif stored.get("refresh_token") and stored.get("email", "").lower() == email.lower():
        session = refresh_session(stored["refresh_token"])
        changed = None
    else:
        raise TigerTagError("Enter the TigerTag password in the plugin's Config tab.")
    write_text(session_path, json.dumps({"email": email, "uid": session["uid"],
                                         "refresh_token": session["refresh_token"]}))
    return session, changed


def sync(capability):
    """The whole sync inside Orca: presets, then filament slots. Returns the message for the user."""
    config = json.loads(capability.get_config() or "{}")
    session, changed_config = authenticate(config, os.path.join(plugin_dir(), SESSION_FILE))
    if changed_config is not None:
        capability.save_config(json.dumps(changed_config))
        config = changed_config

    db = load_database(os.path.join(plugin_dir(), DB_CACHE_FILE))
    spools = spools_from_documents(fetch_inventory(session["uid"], session["id_token"]), db)

    bundle = orca.host.preset_bundle()
    filaments = bundle.filaments
    available = [name for name in filaments.preset_names() if name.endswith("@System")]

    def base_value_of(base_name, key):
        preset = filaments.find_preset(base_name)
        return preset.config_value(key) if preset is not None else None

    presets, skipped = build_presets(spools, available, base_value_of, time.strftime("%Y-%m-%d"))

    data_dir = find_data_dir()
    folder = find_preset_folder(bundle, data_dir)
    write_bundle(os.path.join(data_dir, "user", folder, "_local", BUNDLE_ID), presets)

    if BUNDLE_ID not in orca.host.preset.bundles.list_ids():
        raise TigerTagError(f"The presets were written to the preset folder '{folder}', "
                            "but OrcaSlicer does not list the bundle there.")
    orca.host.preset.bundles.reload_local(BUNDLE_ID)

    message = f"TigerTag: {len(presets)} filament preset(s) from {len(spools)} spool(s)."
    if skipped:
        message += " Skipped: " + "; ".join(skipped) + "."
    try:
        slots = assign_slots(capability, config, session, spools, set(presets))
    except TigerTagError as error:  # the presets are in place; only the slots failed
        slots = f"Slots: {error}"
    if slots:
        message += " " + slots
    return message


def printer_mapping(config):
    """The config's "printers" as a dict; the Config tab may hand it back as JSON text."""
    mapping = config.get("printers")
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping or "{}")
        except ValueError:
            raise TigerTagError('"printers" in the Config tab is not valid JSON.') from None
    return {str(key): str(value or "") for key, value in mapping.items()} if isinstance(mapping, dict) else {}


def assign_slots(capability, config, session, spools, built):
    """Load the spool TigerSpool put into each slot of the active printer. Returns a message or ""."""
    slot_maps = fetch_slot_maps(session["uid"], session["id_token"])
    candidates = sorted(printer_id for printer_id, slots in slot_maps.items() if any(slots.values()))
    if not candidates:
        return ""  # no TigerSpool has published a loaded slot

    bundle = orca.host.preset_bundle()
    printer = bundle.current_printer_preset()
    printer_model = printer.config_value("printer_model") or ""
    mapping = printer_mapping(config)

    names = {}

    def printer_names():
        if not names:
            names.update(fetch_printer_names(session["uid"], session["id_token"]))
        return names

    printer_id, how = choose_printer(printer.name, printer_model, mapping, candidates,
                                     lambda pid: printer_names().get(pid, (None, None)))
    if printer_id is None:
        if how == "configured":
            return ""  # the user said: no TigerSpool slots for this printer
        known = ", ".join(f"{printer_names().get(pid, (None, pid))[1]} = {pid}" for pid in candidates)
        return (f"Slots: not loaded, which TigerTag printer is '{printer.name}'? "
                f"In the Config tab, add it to \"printers\" as \"{printer.name}\": \"<printer id>\" "
                f"({known}).")
    if how == "matched":
        capability.save_config(json.dumps(dict(config, printers=dict(mapping, **{printer.name: printer_id}))))

    filaments = bundle.filaments
    entries = []
    for index in range(filaments.size()):
        preset = filaments.preset(index)
        if preset.is_user():
            entries.append((preset.name, preset.config_value("filament_notes")))
    preset_of = presets_by_uid(spools, preset_names(spools), built, user_copies(entries))

    current = list(bundle.current_filament_preset_names())
    plan, notes = plan_slots(slot_maps.get(printer_id, {}), preset_of.get, len(current))
    loaded = []
    for slot, target, _uid in plan:
        if current[slot - 1] != target:
            try:
                orca.host.preset.filament.begin_update(slot, target)
            except (ValueError, RuntimeError, IndexError) as error:
                notes.append(f"slot {slot}: {error}")
                continue
        loaded.append(f"{slot} {display_name(target)}")

    label = printer_names().get(printer_id, (None, printer_id))[1] if how == "matched" else printer.name
    message = f"Slots of {label}: " + (", ".join(loaded) if loaded else "none loaded")
    if notes:
        message += "; " + "; ".join(notes)
    return message + "."


def notify(text, error=False):
    """Show text as a notification in the main window.

    The ExecutionResult message only reaches Orca's log, not the user.
    """
    try:
        levels = orca.host.ui.NotificationLevel
        level = levels.ErrorNotificationLevel if error else levels.RegularNotificationLevel
        orca.host.ui.push_notification(level, text)
    except (AttributeError, RuntimeError):
        pass  # the result message still lands in the log


if orca is not None:
    class TigerTagSync(orca.script.ScriptPluginCapabilityBase):
        def get_name(self):
            return "TigerTag: sync filament presets"

        def get_default_config(self):
            return {"email": "", "password": "", "printers": {}}

        def execute(self):
            try:
                message = sync(self)
            except TigerTagError as error:
                message = f"TigerTag: {error}"
            except RuntimeError as error:  # raised by the host, e.g. reload_local
                message = f"TigerTag: {error}"
            else:
                notify(message)
                return orca.ExecutionResult.success(message)
            notify(message, error=True)
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, message)

    @orca.plugin
    class TigerTagPlugin(orca.base):
        def register_capabilities(self):
            orca.request_permissions(network=NETWORK_HOSTS)
            orca.register_capability(TigerTagSync)
