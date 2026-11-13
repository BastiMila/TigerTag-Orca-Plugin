"""Run the stage-1 sync outside OrcaSlicer, against the real account.

Signs in with the e-mail and password in LOCAL.md, reads the inventory and writes
the bundle into OUT_DIR instead of Orca's data folder. Base presets are matched
against tests/fixtures/orca_system_filaments.txt; without Orca there are no base
values, so temperatures are not clamped and vectors have one element.

Usage: python -I tools/dry_run.py LOCAL.md OUT_DIR
Prints no token and no password.
"""
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tigertag_orca_plugin as plugin  # noqa: E402


def credentials(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\s*(email|password)\s*:\s*(.*?)\s*$", line)
            if match:
                values[match.group(1)] = match.group(2)
    if not values.get("email") or not values.get("password"):
        sys.exit(f"{path}: email or password missing")
    return values["email"], values["password"]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    local, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    session = plugin.sign_in_with_password(*credentials(local))
    documents = plugin.fetch_inventory(session["uid"], session["id_token"])
    db = plugin.load_database(os.path.join(out_dir, plugin.DB_CACHE_FILE))
    spools = plugin.spools_from_documents(documents, db)

    with open(os.path.join(ROOT, "tests", "fixtures", "orca_system_filaments.txt"), encoding="utf-8") as handle:
        names = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    presets, skipped = plugin.build_presets(spools, names, lambda base, key: None, time.strftime("%Y-%m-%d"))
    plugin.write_bundle(os.path.join(out_dir, plugin.BUNDLE_ID), presets)

    print(f"{len(documents)} documents, {len(spools)} spools, {len(presets)} presets, {len(skipped)} skipped")
    for name, preset in sorted(presets.items()):
        print(f"  {name:55} <- {preset['inherits']}")
    for line in skipped:
        print("  SKIPPED", line)


if __name__ == "__main__":
    try:
        main()
    except plugin.TigerTagError as error:
        sys.exit(f"error: {error}")
