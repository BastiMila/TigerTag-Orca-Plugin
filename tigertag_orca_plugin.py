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

"TigerTag: send slots to TigerSpool" writes users/{uid}/load_requests/{printerId}, the
only write: which spool goes into which slot. Spools and weights are never written.

The module also imports without Orca (orca = None), so the pure functions can be
tested and tools/dry_run.py can run a sync outside the slicer.
"""
import glob
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
STATE_FOLDER = "tigertag_plugin"  # below data_dir(); survives a reinstall of the plugin
SESSION_FILE = ".session.json"
PRINTER_MAP_FILE = "printers.json"  # Orca printer preset -> account printer id, shared by all actions
INVENTORY_CACHE_FILE = "inventory.json"  # written by the sync, read by the TigerTag tab
# A slot map in this file (plugin folder) replaces what the TigerSpools published:
# for testing slot loading without a printer or a TigerSpool.
TEST_SLOTS_FILE = "test_slots.json"
DB_CACHE_FILE = "tigertag_db_cache.json"
DB_TABLES = ("id_brand", "id_material", "id_diameter", "id_aspect")
FILAMENT_TYPE_ID = 142
PRESET_VERSION = "2.5.0.0"

# Only these fields are fetched; inventory documents are large.
INVENTORY_MASK = [
    "deleted", "twin_tag_uid", "id_type", "id_material", "id_brand", "id_aspect1",
    "series", "name", "online_color", "color_r", "color_g", "color_b",
    "data1", "data2", "data3", "data6", "data7",
    "measure_gr", "weight_available", "rack",
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


def http_json(what, url, body=None, token=None, timeout=30, method=None):
    """POST body (or GET without one; or `method`) and return the parsed JSON answer."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
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
        if error.code == 403:
            raise TigerTagError(f"{what} failed: the TigerTag account does not allow this (HTTP 403).") from None
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


def firestore_value(value):
    """Plain Python -> a Firestore REST typed value."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(key): firestore_value(item) for key, item in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [firestore_value(item) for item in value]}}
    return {"stringValue": str(value)}


def write_document(what, path, fields, id_token):
    """Create or replace the document at path with exactly these fields."""
    url = f"{FIRESTORE_URL}/{urllib.parse.quote(path)}"
    body = {"fields": {key: firestore_value(value) for key, value in fields.items()}}
    return http_json(what, url, body, token=id_token, method="PATCH")


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


def _rack(value):
    """The spool's place, {"id", "level", "position"}, or None."""
    if not (isinstance(value, dict) and value.get("id")):
        return None
    return {"id": str(value["id"]), "level": value.get("level"), "position": value.get("position")}


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
        "rack": _rack(fields.get("rack")),
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


def load_test_slots(path):
    """The slot map in the test file, {printer id: {slot key: entry or null}}, or None without one."""
    try:
        with open(path, encoding="utf-8") as handle:
            slot_maps = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise TigerTagError(f"{TEST_SLOTS_FILE} cannot be read: {error}") from None
    if not isinstance(slot_maps, dict):
        raise TigerTagError(f"{TEST_SLOTS_FILE} must hold {{printer id: {{slot: {{\"uid\": ...}}}}}}.")
    return merge_slot_maps([(TEST_SLOTS_FILE, {"slots": slot_maps})])


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


# --------------------------------------------------------------------------- inventory tab

def fetch_rack_names(uid, id_token):
    """Rack id -> its name, from users/{uid}/racks."""
    documents = fetch_documents("Reading the racks", f"users/{uid}/racks", ["name"], id_token)
    return {rack_id: fields.get("name") or rack_id for rack_id, fields in documents}


def rack_label(rack, rack_names):
    """ "Karton Sunlu 250G, B5" for a spool's rack entry, or "".

    Level and position are stored from 0; Tiger Studio shows the level as a letter
    and the position from 1 (level 1, position 4 is B5 - checked by the owner).
    """
    if not rack:
        return ""
    level, position = rack.get("level"), rack.get("position")
    spot = ""
    if isinstance(level, int) and 0 <= level < 26:
        spot += chr(ord("A") + level)
    if isinstance(position, int) and position >= 0:
        spot += str(position + 1)
    name = rack_names.get(rack["id"], rack["id"])
    return f"{name}, {spot}" if spot else name


def inventory_cache(spools, names, built, rack_names, now):
    """What the TigerTag tab shows, written by every sync: the tab itself makes no request."""
    return {
        "synced_at": now,
        "spools": [dict(spool, preset=names.get(spool["uid"]) if names.get(spool["uid"]) in built else None,
                        place=rack_label(spool["rack"], rack_names),
                        rack=dict(spool["rack"], name=rack_names.get(spool["rack"]["id"], ""))
                        if spool["rack"] else None)
                   for spool in spools],
    }


_USED_G = re.compile(r"^;\s*filament used \[g\]\s*=\s*(.+)$", re.MULTILINE)


def parse_filament_used(text):
    """The grams per filament of an Orca G-code ("; filament used [g] = 1.23, 0.00"), or None."""
    match = _USED_G.search(text)
    if not match:
        return None
    grams = []
    for item in match.group(1).split(","):
        try:
            grams.append(float(item))
        except ValueError:
            return None
    return grams


_SETTINGS_ID = re.compile(r"^;\s*filament_settings_id\s*=\s*(.*)$", re.MULTILINE)


def parse_gcode_presets(text):
    """The filament preset per slot an Orca G-code was sliced with, or None."""
    match = _SETTINGS_ID.search(text)
    if not match:
        return None
    return [item.strip().strip('"') for item in match.group(1).split(";")]


def printer_slot_key(orca_slot, known_keys):
    """Orca's 1-based slot -> the printer's own slot key (inverse of orca_slot_number)."""
    zero_based = any(str(key).strip() == "0" for key in known_keys)
    return str(orca_slot - 1 if zero_based else orca_slot)


def build_load_request(rows, sliced, known_keys, now_ms, job):
    """The document for users/{uid}/load_requests/{printerId}: which spool goes into which slot.

    Format agreed with TigerSpool (2026-11-14). Only slots holding a TigerTag spool
    are asked for; after a slice, only the slots the plate uses.
    """
    slots = {}
    for row in rows:
        spool = row["spool"]
        if spool is None or (sliced and not row["needed_g"]):
            continue
        rack = spool.get("rack")
        slots[printer_slot_key(row["slot"], known_keys)] = {
            "uid": spool["uid"],
            "uids": [uid for uid in (spool["uid"], spool.get("twin_uid")) if uid],
            "preset": row["preset"],
            "material": spool.get("material") or "",
            "color": spool.get("colour") or "",
            "grams_needed": round(row["needed_g"], 1) if row["needed_g"] is not None else None,
            "rack": {"id": rack["id"], "name": rack.get("name") or "", "level": rack.get("level"),
                     "position": rack.get("position")} if rack else None,
            "place": spool.get("place") or "",
        }
    return {"source": "orcaslicer", "requested_at": now_ms, "job": job, "slots": slots}


def slot_rows(slot_presets, uid_of_preset, spools, used):
    """One row per filament slot: preset, spool, grams needed, and whether the spool runs short.

    slot_presets: the internal preset name per slot; uid_of_preset(name) -> chip uid or None;
    used: grams per slot of the last slice, or None.
    """
    by_uid = {}
    for spool in spools:
        for uid in (spool["uid"], spool.get("twin_uid")):
            if uid:
                by_uid[uid] = spool
    rows = []
    for index, preset in enumerate(slot_presets):
        spool = by_uid.get(uid_of_preset(preset) or "")
        needed = used[index] if used is not None and index < len(used) else None
        remaining = spool.get("remaining_g") if spool else None
        rows.append({
            "slot": index + 1,
            "preset": display_name(preset),
            "spool": spool,
            "needed_g": needed,
            "short": bool(needed and remaining is not None and needed > remaining),
        })
    return rows


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


def state_dir():
    """<data_dir>/tigertag_plugin: session and database cache.

    Not the plugin folder, which OrcaSlicer empties when the plugin is reinstalled.
    """
    path = os.path.join(find_data_dir(), STATE_FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


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
    session, changed_config = authenticate(config, os.path.join(state_dir(), SESSION_FILE))
    if changed_config is not None:
        capability.save_config(json.dumps(changed_config))
        config = changed_config

    db = load_database(os.path.join(state_dir(), DB_CACHE_FILE))
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

    try:
        rack_names = fetch_rack_names(session["uid"], session["id_token"])
    except TigerTagError:
        rack_names = {}  # the tab then shows rack ids instead of names
    write_text(os.path.join(state_dir(), INVENTORY_CACHE_FILE),
               json.dumps(inventory_cache(spools, preset_names(spools), set(presets), rack_names, int(time.time()))))

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


def load_printer_map():
    try:
        with open(os.path.join(state_dir(), PRINTER_MAP_FILE), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {str(key): str(value or "") for key, value in stored.items()} if isinstance(stored, dict) else {}


def remember_printer(orca_printer, printer_id):
    stored = load_printer_map()
    stored[orca_printer] = printer_id
    write_text(os.path.join(state_dir(), PRINTER_MAP_FILE), json.dumps(stored, indent=2, ensure_ascii=False))


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
    slot_maps = load_test_slots(os.path.join(plugin_dir(), TEST_SLOTS_FILE))
    source = f" (from {TEST_SLOTS_FILE})" if slot_maps is not None else ""
    if slot_maps is None:
        slot_maps = fetch_slot_maps(session["uid"], session["id_token"])
    candidates = sorted(printer_id for printer_id, slots in slot_maps.items() if any(slots.values()))
    if not candidates:
        return f"Slots{source}: no slot holds a spool." if source else ""

    bundle = orca.host.preset_bundle()
    printer = bundle.current_printer_preset()
    printer_model = printer.config_value("printer_model") or ""
    mapping = dict(load_printer_map(), **printer_mapping(config))

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
        remember_printer(printer.name, printer_id)

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
    message = f"Slots of {label}{source}: " + (", ".join(loaded) if loaded else "none loaded")
    if notes:
        message += "; " + "; ".join(notes)
    return message + "."


INVENTORY_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<style>
  :root { --fg: var(--orca-fg, #222); --muted: var(--orca-muted, #777); --line: var(--orca-border, #ddd);
          --warn: #c62828; --ok: #2e7d32; }
  body { margin: 0; padding: 14px 18px; font: 13px system-ui, sans-serif; color: var(--fg); }
  header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  header h2 { margin: 0; font-size: 16px; }
  header .muted { flex: 1; }
  .muted { color: var(--muted); font-size: 12px; }
  h3 { margin: 18px 0 8px; font-size: 14px; }
  .slots { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 8px; }
  .card { border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; }
  .card.short { border-color: var(--warn); }
  .card .top { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .warn { color: var(--warn); font-weight: 600; }
  .sw { width: 14px; height: 14px; border-radius: 3px; border: 1px solid var(--line); flex: none; display: inline-block; vertical-align: middle; }
  input { width: 260px; padding: 4px 6px; margin-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line); }
  th { font-weight: 600; font-size: 12px; color: var(--muted); }
  td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bar { width: 80px; height: 6px; background: var(--line); border-radius: 3px; display: inline-block; vertical-align: middle; margin-left: 6px; }
  .bar i { display: block; height: 100%; border-radius: 3px; background: var(--ok); }
</style>
<header>
  <h2>TigerTag</h2>
  <span class="muted" id="synced"></span>
  <button type="button" id="refresh">Refresh</button>
</header>
<div id="error" class="warn"></div>
<h3>Filament slots</h3>
<div class="muted" id="slicehint"></div>
<div class="slots" id="slots"></div>
<h3>Spools</h3>
<input id="filter" placeholder="Filter: brand, material, colour, place">
<table>
  <thead><tr><th></th><th>Spool</th><th>Left</th><th>Place</th></tr></thead>
  <tbody id="spools"></tbody>
</table>
<script>
  let state = null;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const name = sp => sp.preset || [sp.brand, sp.series || sp.material, sp.colour_name].filter(Boolean).join(' ');
  const grams = g => g == null ? '?' : Math.round(g) + ' g';
  const swatch = c => '<span class="sw" style="background:' + esc(c || 'transparent') + '"></span>';

  function slotCard(row) {
    const sp = row.spool;
    let body;
    if (!sp) {
      body = '<div class="muted">No TigerTag spool</div>';
    } else {
      body = '<div>' + grams(sp.remaining_g) + ' left' + (sp.place ? ' &middot; ' + esc(sp.place) : '') + '</div>';
    }
    if (row.needed_g != null && row.needed_g > 0) {
      body += row.short
        ? '<div class="warn">Needs ' + grams(row.needed_g) + ' - not enough on this spool</div>'
        : '<div>Needs ' + grams(row.needed_g) + '</div>';
    }
    return '<div class="card' + (row.short ? ' short' : '') + '"><div class="top">' + row.slot + ' ' +
      (sp ? swatch(sp.colour) : '') + ' ' + esc(row.preset) + '</div>' + body + '</div>';
  }

  function render() {
    if (!state) return;
    document.getElementById('synced').textContent = state.synced_at
      ? 'Synced ' + new Date(state.synced_at * 1000).toLocaleString()
      : 'Not synced yet: run "TigerTag: sync filament presets" in the Plugins window.';
    document.getElementById('slicehint').textContent = state.sliced
      ? '' : 'Slice the plate to see how much each slot needs.';
    document.getElementById('slots').innerHTML = state.slots.map(slotCard).join('');
    const words = document.getElementById('filter').value.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = state.spools.filter(sp => {
      const text = [name(sp), sp.brand, sp.material, sp.colour_name, sp.place, sp.uid].join(' ').toLowerCase();
      return words.every(w => text.includes(w));
    }).map(sp => {
      const share = sp.measure_g && sp.remaining_g != null ? Math.max(0, Math.min(1, sp.remaining_g / sp.measure_g)) : null;
      return '<tr><td>' + swatch(sp.colour) + '</td><td>' + esc(name(sp)) + '</td><td class="num">' +
        grams(sp.remaining_g) + (sp.measure_g ? ' / ' + grams(sp.measure_g) : '') +
        (share == null ? '' : '<span class="bar"><i style="width:' + Math.round(share * 100) + '%"></i></span>') +
        '</td><td>' + esc(sp.place) + '</td></tr>';
    });
    document.getElementById('spools').innerHTML = rows.join('') ||
      '<tr><td colspan="4" class="muted">No spools.</td></tr>';
  }

  window.orca.onMessage(msg => {
    if (!msg) return;
    if (msg.command === 'state') { state = msg.state; document.getElementById('error').textContent = ''; render(); }
    if (msg.command === 'error') document.getElementById('error').textContent = msg.text;
    if (msg.command === 'changed') refresh();
  });
  // Slots change in the sidebar without telling the page; ask again every few
  // seconds while the tab is visible (the plugin reads presets only when asked).
  function refresh() { window.orca.postMessage({command: 'refresh'}); }
  setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 5000);
  document.getElementById('refresh').onclick = refresh;
  document.getElementById('filter').oninput = render;
  window.orca.postMessage({command: 'ready'});
</script>
"""


def slice_folder():
    """<temp>/orcaslicer_model, where OrcaSlicer writes the G-code of a slice.

    Read from TEMP, not tempfile.gettempdir(): that one probes the folder by
    creating and deleting a file, and every probe raises a permission prompt.
    """
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    return os.path.join(temp, "orcaslicer_model") if temp else ""


_SESSION_DIRS = {}  # (slice folder, pid) -> the Metadata folder of that Orca session


def find_latest_gcode(folder, pid):
    """The G-code OrcaSlicer wrote for the last slice in this session, or None.

    Orca slices into <folder>/<day>/<time>#<pid>#<n>/Metadata/.<pid>.<plate>.gcode
    (a hidden name). The plugin runs inside Orca, so the pid is its own.
    """
    if not folder:
        return None
    key = (folder, pid)
    files = []
    if key in _SESSION_DIRS:  # the folder of this Orca session, found before: no new scan
        files = glob.glob(os.path.join(glob.escape(_SESSION_DIRS[key]), "*.gcode"), include_hidden=True)
    if not files:
        pattern = os.path.join(glob.escape(folder), "*", f"*#{pid}#*", "Metadata", "*.gcode")
        files = glob.glob(pattern, include_hidden=True)
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    _SESSION_DIRS[key] = os.path.dirname(newest)
    return newest


def _gcode_tail(path):
    """The end of a G-code file, where Orca writes the totals and the config."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 512 * 1024))
        return handle.read().decode("utf-8", "replace")


def read_filament_used(path):
    """Grams per filament of a G-code file, or None."""
    try:
        return parse_filament_used(_gcode_tail(path))
    except OSError:
        return None


def last_slice(slot_presets):
    """Grams per slot of the newest slice, or None when there is none or the slots changed since."""
    path = find_latest_gcode(slice_folder(), os.getpid())
    if not path:
        return None
    try:
        tail = _gcode_tail(path)
    except OSError:
        return None
    presets = parse_gcode_presets(tail)
    if presets is not None and presets != list(slot_presets):
        return None  # sliced with other filaments than the slots hold now
    return parse_filament_used(tail)


def inventory_state(used):
    """Everything the TigerTag tab shows, from the sync's cache and the current slots."""
    try:
        with open(os.path.join(state_dir(), INVENTORY_CACHE_FILE), encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        cache = {"synced_at": None, "spools": []}
    spools = cache.get("spools", [])
    preset_uid = {LOCAL_PREFIX + spool["preset"]: spool["uid"] for spool in spools if spool.get("preset")}

    bundle = orca.host.preset_bundle()
    filaments = bundle.filaments

    def uid_of_preset(name):
        if name in preset_uid:
            return preset_uid[name]
        preset = filaments.find_preset(name)
        match = NOTE_UID.search((preset.config_value("filament_notes") or "") if preset is not None else "")
        return match.group(1).upper() if match else None

    slots = list(bundle.current_filament_preset_names())
    return {
        "synced_at": cache.get("synced_at"),
        "spools": spools,
        "slots": slot_rows(slots, uid_of_preset, spools, used),
        "sliced": used is not None,
    }


def send_load_request(capability):
    """Write the active printer's slots as a load request for TigerSpool. Returns the message."""
    stored = load_session(os.path.join(state_dir(), SESSION_FILE))
    if not stored.get("refresh_token"):
        raise TigerTagError("Run \"TigerTag: sync filament presets\" once first; it signs in.")
    session = refresh_session(stored["refresh_token"])
    write_text(os.path.join(state_dir(), SESSION_FILE),
               json.dumps(dict(stored, refresh_token=session["refresh_token"])))

    config = json.loads(capability.get_config() or "{}")
    bundle = orca.host.preset_bundle()
    printer = bundle.current_printer_preset()
    mapping = dict(load_printer_map(), **printer_mapping(config))
    names = fetch_printer_names(session["uid"], session["id_token"])
    printer_id, how = choose_printer(printer.name, printer.config_value("printer_model") or "", mapping,
                                     sorted(names), lambda pid: names.get(pid, (None, None)))
    if printer_id is None:
        known = ", ".join(f"{name} = {pid}" for pid, (_brand, name) in sorted(names.items()))
        raise TigerTagError(f"Which TigerTag printer is '{printer.name}'? In this action's Config tab, add "
                            f"\"printers\": {{\"{printer.name}\": \"<printer id>\"}} ({known}).")
    if how == "matched":
        remember_printer(printer.name, printer_id)

    slot_presets = list(bundle.current_filament_preset_names())
    state = inventory_state(last_slice(slot_presets))
    if not state["spools"]:
        raise TigerTagError("Run \"TigerTag: sync filament presets\" first; there is no inventory yet.")
    known_keys = list(fetch_slot_maps(session["uid"], session["id_token"]).get(printer_id, {}))
    request = build_load_request(state["slots"], state["sliced"], known_keys, int(time.time() * 1000),
                                 time.strftime("%Y-%m-%d %H:%M"))
    if not request["slots"]:
        raise TigerTagError("No filament slot holds a TigerTag spool"
                            + (" that this plate uses." if state["sliced"] else "."))
    write_document("Sending the load request", f"users/{session['uid']}/load_requests/{printer_id}",
                   request, session["id_token"])
    label = names.get(printer_id, (None, printer_id))[1]
    parts = [f"{key} {slot['preset']}" + (f" ({slot['grams_needed']} g)" if slot["grams_needed"] else "")
             for key, slot in sorted(request["slots"].items(), key=lambda item: (len(item[0]), item[0]))]
    return f"TigerTag: sent to {label}: " + ", ".join(parts) + "."


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

    class TigerTagSend(orca.script.ScriptPluginCapabilityBase):
        def get_name(self):
            return "TigerTag: send slots to TigerSpool"

        def get_default_config(self):
            return {"printers": {}}

        def execute(self):
            try:
                message = send_load_request(self)
            except (TigerTagError, RuntimeError) as error:
                message = f"TigerTag: {error}"
                notify(message, error=True)
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, message)
            notify(message)
            return orca.ExecutionResult.success(message)

    class TigerTagInventory(orca.pages.PagesPluginCapabilityBase):
        """The TigerTag tab: spools, places, what is left, and the slots of the last slice."""

        def get_name(self):
            return "TigerTag"

        def get_ui(self):
            return INVENTORY_PAGE

        def post_state(self):
            try:
                slots = list(orca.host.preset_bundle().current_filament_preset_names())
                self.post_message({"command": "state", "state": inventory_state(last_slice(slots))})
            except (TigerTagError, RuntimeError, OSError) as error:
                self.post_message({"command": "error", "text": str(error)})

        def on_message(self, message):
            # Orca hands the page's message over already parsed (a dict), not as JSON text.
            if isinstance(message, str):
                try:
                    message = json.loads(message)
                except ValueError:
                    return
            if isinstance(message, dict) and message.get("command") in ("ready", "refresh"):
                self.post_state()

        def on_lifecycle_event(self, event, ctx):
            # Touch nothing here: events fire in the middle of preset switches and AMS syncs.
            # The page is only told to ask again; its request arrives later, when Orca is idle.
            if event == orca.LifecycleEvent.SlicingJobComplete:
                self.post_message({"command": "changed"})

    @orca.plugin
    class TigerTagPlugin(orca.base):
        def register_capabilities(self):
            # One grant for the whole slice folder, asked once at load: the tab reads
            # the G-code of each slice there. Without it every folder read prompts.
            folder = slice_folder()
            orca.request_permissions(network=NETWORK_HOSTS, fs_read=[os.path.join(folder, "*")] if folder else [])
            orca.register_capability(TigerTagSync)
            orca.register_capability(TigerTagSend)
            orca.register_capability(TigerTagInventory)
