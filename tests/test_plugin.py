"""Tests of the parts of the plugin that do not need OrcaSlicer. Run: python -m unittest -v"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tigertag_orca_plugin as plugin  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")

DB = {
    "id_brand": {51857: {"id": 51857, "name": "Sunlu"}, 7: {"id": 7, "name": "Acme"}},
    "id_material": {
        38219: {"id": 38219, "label": "PLA", "material_type": "PLA", "density": 1.24},
        500: {"id": 500, "label": "PETG", "material_type": "PETG", "density": 1.27},
        900: {"id": 900, "label": "Unobtainium", "material_type": "Unobtainium", "density": 2.0},
    },
    "id_diameter": {56: {"id": 56, "label": "1.75"}},
    "id_aspect": {104: {"id": 104, "label": "Basic"}, 105: {"id": 105, "label": "Matte"},
                  255: {"id": 255, "label": "None"}},
}


def document(**overrides):
    """An inventory document shaped like the one read from the real account (values changed)."""
    fields = {
        "id_type": 142, "id_material": 38219, "id_brand": 51857, "id_aspect1": 104,
        "series": "PLA+", "name": "Olive Green", "online_color": "#5A6D30FF",
        "color_r": 90, "color_g": 109, "color_b": 48,
        "data1": 56, "data2": 190, "data3": 240, "data6": 25, "data7": 65,
        "measure_gr": 1000, "weight_available": 250, "twin_tag_uid": None,
        "catalogProduct": {"brand": "Sunlu", "material": "PLA", "series": "PLA+",
                           "filament": {"diameter": "1.75"}},
    }
    fields.update(overrides)
    return fields


def system_names():
    with open(os.path.join(FIXTURES, "orca_system_filaments.txt"), encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


class FirestoreTest(unittest.TestCase):
    def test_plain_values(self):
        typed = {"mapValue": {"fields": {
            "a": {"integerValue": "5"}, "b": {"stringValue": "x"}, "c": {"nullValue": None},
            "d": {"arrayValue": {"values": [{"booleanValue": True}]}}, "e": {"arrayValue": {}},
        }}}
        self.assertEqual(plugin.firestore_plain(typed), {"a": 5, "b": "x", "c": None, "d": [True], "e": []})


class SpoolTest(unittest.TestCase):
    def test_fields_of_a_catalog_spool(self):
        spool = plugin.spool_from_document("5301e4ba000001", document(), DB)
        self.assertEqual(spool["uid"], "5301E4BA000001")
        self.assertEqual((spool["brand"], spool["material"], spool["series"]), ("Sunlu", "PLA", "PLA+"))
        self.assertEqual(spool["colour"], "#5A6D30")
        self.assertEqual(spool["diameter"], "1.75")
        self.assertEqual(spool["density"], 1.24)
        self.assertEqual(spool["nozzle"], (190, 240))
        self.assertEqual(spool["bed"], (25, 65))
        self.assertEqual((spool["remaining_g"], spool["measure_g"]), (250, 1000))

    def test_spool_without_catalog_uses_the_database(self):
        fields = document(catalogProduct=None, series="", online_color="", id_brand=7)
        spool = plugin.spool_from_document("AA", fields, DB)
        self.assertEqual((spool["brand"], spool["series"], spool["colour"]), ("Acme", "", "#5A6D30"))

    def test_unknown_temperatures_are_none(self):
        spool = plugin.spool_from_document("AA", document(data2=0, data3=0, data6=70, data7=60), DB)
        self.assertIsNone(spool["nozzle"])
        self.assertIsNone(spool["bed"])  # min above max

    def test_deleted_and_other_types_are_dropped(self):
        self.assertIsNone(plugin.spool_from_document("AA", document(deleted=True), DB))
        self.assertIsNone(plugin.spool_from_document("AA", document(id_type=173), DB))

    def test_twin_tag_spool_counts_once(self):
        docs = [("BB", document(twin_tag_uid="aa")), ("AA", document(twin_tag_uid="BB")), ("CC", document())]
        self.assertEqual([spool["uid"] for spool in plugin.spools_from_documents(docs, DB)], ["AA", "CC"])


class PresetTest(unittest.TestCase):
    def spool(self, **overrides):
        return plugin.spool_from_document(overrides.pop("uid", "5301E4BA000001"), document(**overrides), DB)

    def test_base_prefers_brand_and_series(self):
        self.assertEqual(plugin.choose_base(self.spool(), system_names()), "SUNLU PLA+ @System")

    def test_base_falls_back_to_generic_material(self):
        self.assertEqual(plugin.choose_base(self.spool(id_brand=7, catalogProduct=None, id_material=500,
                                                       series=""), system_names()), "Generic PETG @System")

    def test_base_uses_the_aspect(self):
        spool = self.spool(id_brand=7, catalogProduct=None, series="", id_aspect1=105)
        self.assertEqual(plugin.choose_base(spool, system_names()), "Generic PLA Matte @System")

    def test_no_base_for_an_unknown_material(self):
        spool = self.spool(id_brand=7, catalogProduct=None, series="", id_material=900)
        presets, skipped = plugin.build_presets([spool], system_names(), lambda base, key: None, "2026-11-13")
        self.assertEqual(presets, {})
        self.assertEqual(len(skipped), 1)

    def test_names_are_unique_and_file_safe(self):
        spools = [self.spool(uid="11112222640001", name="Black/White"),
                  self.spool(uid="33334444640001", name="Black/White"),
                  self.spool(uid="5555666601", name="Sunlu Olive Green"),
                  self.spool(uid="5555666602", name="Olive Green")]
        names = plugin.preset_names(spools)
        self.assertEqual(names["11112222640001"], "Sunlu PLA+ Black-White (1111)")   # same end, start differs
        self.assertEqual(names["33334444640001"], "Sunlu PLA+ Black-White (3333)")
        # brand dropped from the colour name; same start, end differs
        self.assertEqual(names["5555666601"], "Sunlu PLA+ Olive Green (6601)")
        self.assertEqual(names["5555666602"], "Sunlu PLA+ Olive Green (6602)")

    def test_tigerdata_ids_and_brand_inside_the_series(self):
        spools = [self.spool(uid="TigerData_17945008581696194"), self.spool(uid="TigerData_17945008630405988"),
                  self.spool(uid="X1", series="ecoPLA Sunlu", name="Neon Orange")]
        names = plugin.preset_names(spools)
        self.assertEqual(names["TIGERDATA_17945008581696194"], "Sunlu PLA+ Olive Green (6194)")
        self.assertEqual(names["X1"], "Sunlu ecoPLA Neon Orange")

    def test_single_spool_has_no_uid_in_its_name(self):
        self.assertEqual(plugin.preset_names([self.spool()]), {"5301E4BA000001": "Sunlu PLA+ Olive Green"})

    def test_base_for_a_two_word_brand_and_by_series(self):
        bambu = self.spool(id_brand=7, catalogProduct={"brand": "Bambu Lab"}, series="PLA Basic")
        self.assertEqual(plugin.choose_base(bambu, system_names()), "Bambu PLA Basic @System")
        hf = self.spool(id_brand=7, catalogProduct=None, series="PETG HF", id_material=500)
        self.assertEqual(plugin.choose_base(hf, system_names()), "Generic PETG HF @System")

    def test_preset_content(self):
        base = {
            "nozzle_temperature": "250,250", "nozzle_temperature_initial_layer": "220,220",
            "nozzle_temperature_range_low": "190,190", "nozzle_temperature_range_high": "230,230",
            "hot_plate_temp": "70", "cool_plate_temp": "35", "eng_plate_temp": "0",
            "filament_diameter": "1.75,1.75", "filament_density": "1.26",
        }
        preset = plugin.build_preset(self.spool(), "X", "SUNLU PLA+ @System", base.get, "2026-11-13")
        self.assertEqual(preset["inherits"], "SUNLU PLA+ @System")
        self.assertEqual(preset["filament_settings_id"], ["X"])
        self.assertEqual(preset["default_filament_colour"], ["#5A6D30"])
        self.assertEqual(preset["filament_vendor"], ["Sunlu"])
        self.assertEqual(preset["filament_diameter"], ["1.75", "1.75"])
        self.assertEqual(preset["filament_density"], ["1.24"])
        self.assertEqual(preset["nozzle_temperature_range_low"], ["190", "190"])
        self.assertEqual(preset["nozzle_temperature_range_high"], ["240", "240"])
        self.assertEqual(preset["nozzle_temperature"], ["240", "240"])      # clamped from 250
        self.assertNotIn("nozzle_temperature_initial_layer", preset)        # 220 already fits
        self.assertEqual(preset["hot_plate_temp"], ["65"])                  # clamped from 70
        self.assertNotIn("cool_plate_temp", preset)                         # 35 fits
        self.assertNotIn("eng_plate_temp", preset)                          # 0 = plate not supported
        self.assertEqual(preset["filament_notes"],
                         ["TigerTag spool 5301E4BA000001: 250 g of 1000 g left (synced 2026-11-13)."])
        json.dumps(preset)  # serialisable


class BundleTest(unittest.TestCase):
    def test_write_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = os.path.join(tmp, plugin.BUNDLE_ID)
            plugin.write_bundle(bundle_dir, {"A": {"name": "A"}, "B": {"name": "B"}}, now=100)
            plugin.write_bundle(bundle_dir, {"B": {"name": "B"}}, now=200)
            self.assertEqual(sorted(os.listdir(os.path.join(bundle_dir, "filament"))), ["B.json"])
            with open(os.path.join(bundle_dir, "bundle_metadata.json"), encoding="utf-8") as handle:
                metadata = json.load(handle)
            self.assertEqual(metadata["id"], plugin.BUNDLE_ID)
            self.assertEqual(metadata["filament_presets"], ["B"])
            self.assertEqual((metadata["imported_time"], metadata["updated_time"]), (100, 200))


class DatabaseTest(unittest.TestCase):
    def test_cache_is_used_when_the_download_fails(self):
        rows = {table: [{"id": 1, "label": table}] for table in plugin.DB_TABLES}
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "db.json")
            plugin.load_database(cache, fetch=lambda table: rows[table])

            def offline(table):
                raise plugin.TigerTagError("offline")
            db = plugin.load_database(cache, fetch=offline)
            self.assertEqual(db["id_material"][1]["label"], "id_material")
            os.remove(cache)
            with self.assertRaises(plugin.TigerTagError):
                plugin.load_database(cache, fetch=offline)


class AuthenticateTest(unittest.TestCase):
    def test_password_is_used_once_then_the_refresh_token(self):
        calls = []
        saved = (plugin.sign_in_with_password, plugin.refresh_session)
        plugin.sign_in_with_password = lambda e, p: calls.append("password") or {
            "uid": "U", "id_token": "I1", "refresh_token": "R1"}
        plugin.refresh_session = lambda r: calls.append("refresh:" + r) or {
            "uid": "U", "id_token": "I2", "refresh_token": "R2"}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "s.json")
                session, changed = plugin.authenticate({"email": "a@b.c", "password": "pw"}, path)
                self.assertEqual(changed, {"email": "a@b.c", "password": ""})
                session, changed = plugin.authenticate(changed, path)
                self.assertIsNone(changed)
                self.assertEqual(session["id_token"], "I2")
                self.assertEqual(calls, ["password", "refresh:R1"])
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)
                self.assertNotIn("id_token", stored)
                self.assertEqual(stored["refresh_token"], "R2")
                with self.assertRaises(plugin.TigerTagError):
                    plugin.authenticate({"email": "other@b.c", "password": ""}, path)
        finally:
            plugin.sign_in_with_password, plugin.refresh_session = saved


class SlotTest(unittest.TestCase):
    def test_newest_entry_wins_over_older_and_null(self):
        old = {"uid": "AA01", "material": "PLA", "color": "#000000", "set_at": 100}
        new = {"uid": "BB02", "material": "PLA", "color": "#000000", "set_at": 200}
        boxes = [("mac1", {"slots": {"p1": {"1": old, "2": None, "3": None}}}),
                 ("mac2", {"slots": {"p1": {"1": new, "2": old}, "p2": {"1": None}}}),
                 ("mac3", {})]
        merged = plugin.merge_slot_maps(boxes)
        self.assertEqual(merged["p1"], {"1": new, "2": old, "3": None})
        self.assertEqual(merged["p2"], {"1": None})

    def test_slot_numbers(self):
        self.assertEqual(plugin.orca_slot_number("1", ["1", "2"]), 1)
        self.assertEqual(plugin.orca_slot_number("0", ["0", "3"]), 1)  # Happy Hare gate 0
        self.assertEqual(plugin.orca_slot_number("3", ["0", "3"]), 4)
        self.assertIsNone(plugin.orca_slot_number("ext", ["ext"]))

    def test_printer_by_brand_or_name_mapping_or_ambiguity(self):
        printers = {"ff": ("flashforge", "Workshop"), "kb": ("anycubic", "Kobra"),
                    "ff2": ("flashforge", "Second"), "c5": ("klipper", "FlashForge Creator 5 Pro"),
                    "test": ("klipper", "Testprinter")}.get
        model = "Flashforge Creator 5 Pro"
        self.assertEqual(plugin.choose_printer("C5", model, {}, ["ff", "kb"], printers), ("ff", "matched"))
        self.assertEqual(plugin.choose_printer("C5", model, {}, ["c5", "test"], printers), ("c5", "matched"))
        self.assertEqual(plugin.choose_printer("C5", model, {}, ["ff", "ff2"], printers), (None, "ambiguous"))
        self.assertEqual(plugin.choose_printer("C5", model, {}, ["kb", "test"], printers), (None, "none"))
        self.assertEqual(plugin.choose_printer("C5", model, {"C5": "ff2"}, ["ff", "ff2"], printers), ("ff2", "configured"))
        self.assertEqual(plugin.choose_printer("C5", model, {"C5": ""}, ["ff"], printers), (None, "configured"))
        self.assertTrue(plugin.printer_matches("bambulab", "X1", "Bambu Lab X1 Carbon"))
        self.assertFalse(plugin.printer_matches("klipper", "Bench", "Voron 2.4 klipper"))
        self.assertFalse(plugin.printer_matches("flashforge", "A", ""))

    def test_user_copy_wins_and_twin_chip_maps_to_the_spool(self):
        spools = plugin.spools_from_documents(
            [("aa01", document(twin_tag_uid="aa02")), ("bb01", document(name="Red"))], DB)
        names = plugin.preset_names(spools)
        copies = plugin.user_copies([
            ("_local/tigertag/Sunlu PLA+ Red", "TigerTag spool BB01: 1 g"),
            ("My Olive", '"TigerTag spool AA01: 250 g of 1000 g left (synced 2026-11-13)."'),
            ("Copy A", "TigerTag spool CC01"), ("Copy B", "TigerTag spool CC01"),
        ])
        self.assertEqual(copies, {"AA01": "My Olive"})
        preset_of = plugin.presets_by_uid(spools, names, set(names.values()), copies)
        self.assertEqual(preset_of["AA01"], "My Olive")
        self.assertEqual(preset_of["AA02"], "My Olive")
        self.assertEqual(preset_of["BB01"], "_local/tigertag/Sunlu PLA+ Red")
        self.assertEqual(plugin.display_name(preset_of["BB01"]), "Sunlu PLA+ Red")

    def test_printer_mapping_as_dict_or_text(self):
        self.assertEqual(plugin.printer_mapping({"printers": {"C5": "abc"}}), {"C5": "abc"})
        self.assertEqual(plugin.printer_mapping({"printers": '{"C5": "abc"}'}), {"C5": "abc"})
        self.assertEqual(plugin.printer_mapping({}), {})

    def test_printer_choice_from_the_tab(self):
        stored = {"C5": "abc", "Voron": ""}
        self.assertEqual(plugin.printer_choice(stored, "C5", None), {"Voron": ""})  # automatic again
        self.assertEqual(plugin.printer_choice(stored, "C5", ""), {"C5": "", "Voron": ""})  # no printer
        self.assertEqual(plugin.printer_choice(stored, "New", "xyz")["New"], "xyz")
        self.assertEqual(stored, {"C5": "abc", "Voron": ""})  # not changed in place
        self.assertEqual(plugin.choose_printer("C5", "", plugin.printer_choice(stored, "C5", ""),
                                               ["abc"], lambda _pid: (None, None)), (None, "configured"))
        with self.assertRaises(plugin.TigerTagError):
            plugin.printer_mapping({"printers": "{nope"})

    def test_test_slots_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, plugin.TEST_SLOTS_FILE)
            self.assertIsNone(plugin.load_test_slots(path))
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"p1": {"1": {"uid": "AA01"}, "2": None}}, handle)
            self.assertEqual(plugin.load_test_slots(path), {"p1": {"1": {"uid": "AA01"}, "2": None}})
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("[1]")
            with self.assertRaises(plugin.TigerTagError):
                plugin.load_test_slots(path)

    def test_plan(self):
        entry = lambda uid: {"uid": uid, "material": "PLA", "color": "#5A6D30", "set_at": 1}
        slot_map = {"2": entry("bb01"), "1": entry("AA01"), "3": None, "4": entry("ZZ99"), "10": entry("AA01")}
        preset_of = {"AA01": "A", "BB01": "B"}.get
        plan, notes = plugin.plan_slots(slot_map, preset_of, 4)
        self.assertEqual(plan, [(1, "A", "AA01"), (2, "B", "BB01")])
        self.assertEqual(notes, ["slot 4: spool ZZ99 has no TigerTag preset",
                                 "slot 10: the project has only 4 filament(s)"])


if __name__ == "__main__":
    unittest.main()


class InventoryTest(unittest.TestCase):
    def test_rack_and_place(self):
        spool = plugin.spool_from_document("aa01", document(rack={"id": "r1", "level": 1, "position": 3}), DB)
        self.assertEqual(spool["rack"], {"id": "r1", "level": 1, "position": 3})
        self.assertEqual(plugin.rack_label(spool["rack"], {"r1": "Regal"}), "Regal, B4")
        self.assertEqual(plugin.rack_label({"id": "r9", "level": None, "position": 0}, {}), "r9, 1")
        self.assertEqual(plugin.rack_label(None, {}), "")
        self.assertIsNone(plugin.spool_from_document("aa01", document(rack=None), DB)["rack"])

    def test_cache_holds_preset_and_place(self):
        spools = plugin.spools_from_documents([("aa01", document(rack={"id": "r1", "level": 0, "position": 0}))], DB)
        names = plugin.preset_names(spools)
        cache = plugin.inventory_cache(spools, names, set(names.values()), {"r1": "Regal"}, 5)
        self.assertEqual(cache["synced_at"], 5)
        self.assertEqual(cache["spools"][0]["preset"], "Sunlu PLA+ Olive Green")
        self.assertEqual(cache["spools"][0]["place"], "Regal, A1")
        self.assertIsNone(plugin.inventory_cache(spools, names, set(), {}, 5)["spools"][0]["preset"])
        self.assertEqual(cache["printers"], [])
        printers = {"p2": ("klipper", "testprinter"), "p1": ("klipper", "FlashForge Creator 5 Pro")}
        listed = plugin.inventory_cache(spools, names, set(), {}, 5, printers)["printers"]
        self.assertEqual([p["id"] for p in listed], ["p1", "p2"])  # by name, case-insensitive
        self.assertEqual(listed[1], {"id": "p2", "brand": "klipper", "name": "testprinter"})
        json.dumps(cache)  # the tab gets it as JSON

    def test_filament_used(self):
        text = "\n".join(["G1 X1", "; filament used [mm] = 100.0, 0.0",
                          "; filament used [g] = 12.34, 0.00, 250.5", ""])
        self.assertEqual(plugin.parse_filament_used(text), [12.34, 0.0, 250.5])
        self.assertIsNone(plugin.parse_filament_used("G1 X1"))

    def test_slot_rows_warn_when_short(self):
        spools = plugin.spools_from_documents(
            [("aa01", document(twin_tag_uid="aa02")), ("bb01", document(name="Red", weight_available=900))], DB)
        uid_of = {"_local/tigertag/A": "AA02", "_local/tigertag/B": "BB01"}.get
        rows = plugin.slot_rows(["_local/tigertag/A", "_local/tigertag/B", "Generic PLA @System"], uid_of, spools,
                                [300.0, 300.0, 5.0])
        self.assertEqual([(r["slot"], r["preset"], r["short"]) for r in rows],
                         [(1, "A", True), (2, "B", False), (3, "Generic PLA @System", False)])
        self.assertEqual(rows[0]["spool"]["uid"], "AA01")
        self.assertIsNone(rows[2]["spool"])
        self.assertIsNone(plugin.slot_rows(["x"], uid_of, spools, None)[0]["needed_g"])

    def test_latest_gcode_of_this_process_with_a_hidden_name(self):
        with tempfile.TemporaryDirectory() as folder:
            def gcode(session, name, mtime):
                path = os.path.join(folder, "Sat_Nov_14", session, "Metadata", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    print("; filament used [g] = 1.0", file=handle)
                os.utime(path, (mtime, mtime))
                return path
            gcode("19_45_29#27076#63", ".27076.0.gcode", 100)
            newest = gcode("20_29_43#27076#586", ".27076.1.gcode", 200)
            gcode("21_00_00#999#1", ".999.0.gcode", 300)  # another Orca process
            self.assertEqual(plugin.find_latest_gcode(folder, 27076), newest)
            self.assertEqual(plugin.read_filament_used(newest), [1.0])
            self.assertIsNone(plugin.find_latest_gcode(folder, 1))
            self.assertIsNone(plugin.find_latest_gcode("", 27076))


class LoadRequestTest(unittest.TestCase):
    def test_firestore_value(self):
        value = plugin.firestore_value({"a": 1, "b": [1.5, "x", None, True], "c": {}})
        self.assertEqual(value, {"mapValue": {"fields": {
            "a": {"integerValue": "1"},
            "b": {"arrayValue": {"values": [{"doubleValue": 1.5}, {"stringValue": "x"},
                                            {"nullValue": None}, {"booleanValue": True}]}},
            "c": {"mapValue": {"fields": {}}}}}})
        self.assertEqual(plugin.firestore_plain(plugin.firestore_value({"a": [1, None]})), {"a": [1, None]})

    def test_gcode_presets(self):
        text = '; filament_settings_id = "_local/tigertag/A B";"_local/tigertag/C";"Mine Selbst"\n'
        self.assertEqual(plugin.parse_gcode_presets(text), ["_local/tigertag/A B", "_local/tigertag/C", "Mine Selbst"])
        self.assertIsNone(plugin.parse_gcode_presets("G1\n"))

    def test_slot_keys(self):
        self.assertEqual(plugin.printer_slot_key(1, []), "1")
        self.assertEqual(plugin.printer_slot_key(1, ["0", "2"]), "0")

    def test_request_holds_used_tigertag_slots_only(self):
        spools = plugin.spools_from_documents(
            [("aa01", document(twin_tag_uid="aa02", rack={"id": "r1", "level": 0, "position": 2})),
             ("bb01", document(name="Red"))], DB)
        names = plugin.preset_names(spools)
        cached = plugin.inventory_cache(spools, names, set(names.values()), {"r1": "Regal"}, 5)["spools"]
        uid_of = {"_local/tigertag/A": "AA01", "_local/tigertag/B": "BB01"}.get
        rows = plugin.slot_rows(["_local/tigertag/A", "_local/tigertag/B", "Generic PLA @System"],
                                uid_of, cached, [12.34, 0.0, 3.0])
        request = plugin.build_load_request(rows, True, [], 1000, "job")
        self.assertEqual(sorted(request["slots"]), ["1"])  # B is unused, slot 3 has no TigerTag spool
        slot = request["slots"]["1"]
        self.assertEqual(slot["uids"], ["AA01", "AA02"])
        self.assertEqual(slot["grams_needed"], 12.3)
        self.assertEqual(slot["rack"], {"id": "r1", "name": "Regal", "level": 0, "position": 2})
        self.assertEqual((slot["material"], slot["color"], slot["place"]), ("PLA", "#5A6D30", "Regal, A3"))
        self.assertEqual((request["source"], request["requested_at"], request["job"]), ("orcaslicer", 1000, "job"))
        unsliced = plugin.build_load_request(rows, False, [], 1000, "job")
        self.assertEqual(sorted(unsliced["slots"]), ["1", "2"])
        self.assertIsNone(unsliced["slots"]["2"]["rack"])
        plugin.firestore_value(request)  # encodable

        held = {"0": {"uid": "aa02"}, "1": {"uid": "CC01"}, "2": None}  # Happy Hare: gates from 0
        preview = plugin.load_request_preview(rows, False, "p1", held)
        self.assertEqual(preview["printer_id"], "p1")
        self.assertEqual([(s["key"], s["loaded"]) for s in preview["slots"]], [("0", True), ("1", False)])
        self.assertEqual(preview["slots"][0]["place"], slot["place"])
        self.assertEqual(plugin.load_request_preview(rows, True, None, {})["slots"][0]["key"], "1")

    def test_request_goes_into_every_box_that_knows_the_printer(self):
        request = {"source": "orcaslicer", "slots": {"1": {"uid": "AA01"}}}
        boxes = [("mac1", {"printer_ids": ["46UAA607urZDtk62YJgq", "x"]}),
                 ("mac2", {"printer_ids": ["x"]}),
                 ("mac3", {"printer_ids": ["46UAA607urZDtk62YJgq"]}),
                 ("mac4", {})]
        writes = plugin.load_request_writes("u1", boxes, "46UAA607urZDtk62YJgq", request)
        self.assertEqual([w["update"]["name"].rsplit("/", 1)[1] for w in writes], ["mac1", "mac3"])
        write = writes[0]
        self.assertTrue(write["update"]["name"].endswith("/documents/users/u1/tigerspools/mac1"))
        self.assertEqual(write["updateMask"], {"fieldPaths": ["load_requests.`46UAA607urZDtk62YJgq`"]})
        self.assertEqual(write["currentDocument"], {"exists": True})
        fields = write["update"]["fields"]["load_requests"]["mapValue"]["fields"]
        self.assertEqual(plugin.firestore_plain(fields["46UAA607urZDtk62YJgq"]), request)
        self.assertEqual(plugin.load_request_writes("u1", boxes, "nobody", request), [])

    def test_field_path_segment(self):
        self.assertEqual(plugin.field_path_segment("TigerData_1"), "TigerData_1")
        self.assertEqual(plugin.field_path_segment("abc_1"), "abc_1")
        self.assertEqual(plugin.field_path_segment("7mj1K6MF1ucLHeotUlF1"), "`7mj1K6MF1ucLHeotUlF1`")
        self.assertEqual(plugin.field_path_segment("a`b\\c"), "`a\\`b\\\\c`")
