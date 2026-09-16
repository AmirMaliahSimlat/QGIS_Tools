# -*- coding: utf-8 -*-
"""
Sync flat copies of QGIS Processing scripts into scripts/qgis_processing/.

Edit tools in their normal folders (tree_points/, mask_points/, …), then run:

    python scripts/sync_qgis_processing.py

Copy everything inside scripts/qgis_processing/ into:

    %APPDATA%\\QGIS\\QGIS3\\profiles\\default\\processing\\scripts\\
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
DEST = SCRIPTS_ROOT / "qgis_processing"

# Canonical source → flat name in qgis_processing/ (and in the QGIS scripts folder).
DEPLOY_FILES = [
    # Shared helpers
    ("atomic_io.py", "atomic_io.py"),
    ("parallel_util.py", "parallel_util.py"),
    ("quantized_mesh.py", "quantized_mesh.py"),
    ("crs_util.py", "crs_util.py"),
    # Processing algorithms + their side modules
    ("tree_points/tree_mask_to_points.py", "tree_mask_to_points.py"),
    ("tree_points/thin_tree_points.py", "thin_tree_points.py"),
    ("tree_points/sample_tree_rgb.py", "sample_tree_rgb.py"),
    ("tree_points/rgb_core.py", "rgb_core.py"),
    ("building_altitude/building_altitude_and_height.py", "building_altitude_and_height.py"),
    ("mask_points/polygon_mask_points.py", "polygon_mask_points.py"),
    ("roof_type/assign_roof_type.py", "assign_roof_type.py"),
    ("roof_type/roof_type_core.py", "roof_type_core.py"),
    ("layers_alignment/layers_alignment.py", "layers_alignment.py"),
    ("line_of_sight/line_of_sight_checker.py", "line_of_sight_checker.py"),
    ("line_of_sight/los_core.py", "los_core.py"),
]


def sync(*, clean: bool = False) -> list[Path]:
    DEST.mkdir(parents=True, exist_ok=True)
    if clean:
        for old in DEST.glob("*.py"):
            old.unlink()

    written: list[Path] = []
    missing: list[str] = []
    for rel_src, dest_name in DEPLOY_FILES:
        src = SCRIPTS_ROOT / rel_src
        if not src.is_file():
            missing.append(rel_src)
            continue
        out = DEST / dest_name
        shutil.copy2(src, out)
        written.append(out)

    readme = DEST / "README.txt"
    readme.write_text(
        "Copy ALL .py files in this folder into:\r\n"
        "  %APPDATA%\\QGIS\\QGIS3\\profiles\\default\\processing\\scripts\\\r\n"
        "\r\n"
        "Do not copy this README.txt (optional).\r\n"
        "Regenerate this folder after editing tools:\r\n"
        "  python scripts/sync_qgis_processing.py\r\n",
        encoding="utf-8",
    )
    if missing:
        raise FileNotFoundError(
            "Missing source files:\n  " + "\n  ".join(missing)
        )
    return written


def main(argv: list[str]) -> int:
    clean = "--clean" in argv
    written = sync(clean=clean)
    print(f"Synced {len(written)} files -> {DEST}")
    for p in written:
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
