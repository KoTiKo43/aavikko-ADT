#!/usr/bin/env python3
"""
generate_showcase_map.py — Generate an SS14 map that displays ALL Aavikko
modified prototype IDs as spawned entities in a grid.

Only spawns prototypes that are:
- type: entity (not abstract, not gameRule, not gameMap, etc.)
- NOT marked as abstract: true
- Have a parent (inherited from real entity) or have components directly

Uses empty.yml as base template, inserts entities before "..." marker.

Usage:
    python3 generate_showcase_map.py
    python3 generate_showcase_map.py --spacing 2.0
    python3 generate_showcase_map.py --list-only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Patterns
TYPE_PATTERN = re.compile(r'^\s*-?\s*type:\s*["\']?([^"\'\s#]+)', re.MULTILINE)
ID_PATTERN = re.compile(r'^\s*-?\s*id:\s*["\']?([^"\'\s#]+)')
ABSTRACT_PATTERN = re.compile(r'^\s*abstract:\s*true', re.MULTILINE)

# Only spawn entity prototypes — skip everything else
SPAWNABLE_TYPES = {'entity'}

DEFAULT_SPACING = 1.5
DEFAULT_ITEMS_PER_ROW = 20
DEFAULT_START_X = 2.5
DEFAULT_START_Y = 2.5


def extract_prototype_ids(yml_dirs):
    """Extract only spawnable entity prototype IDs.

    Filters out:
    - Non-entity types (gameRule, gameMap, stationEvent, etc.)
    - Abstract prototypes (abstract: true)
    - Base/abstract parents (names starting with "Base")
    """
    ids = set()

    for yml_dir in yml_dirs:
        if not yml_dir.exists():
            continue
        for yml_file in sorted(yml_dir.rglob("*.yml")):
            if yml_file.name in ("manifest.yml", "attributions.yml"):
                continue
            try:
                content = yml_file.read_text(encoding="utf-8")
            except OSError:
                continue

            # Parse line by line — track type, id, abstract per prototype block
            current_type = None
            current_id = None
            current_abstract = False

            for line in content.split('\n'):
                stripped = line.strip()
                if stripped.startswith('- type:'):
                    # Save previous entity
                    if current_type == 'entity' and current_id and not current_abstract:
                        if not current_id.startswith('Base'):
                            ids.add(current_id)
                    # Start new block
                    m = re.match(r'- type:\s*["\']?([^"\'\s#]+)', stripped)
                    current_type = m.group(1) if m else None
                    current_id = None
                    current_abstract = False
                elif stripped.startswith('id:') and current_type:
                    m = re.match(r'id:\s*["\']?([^"\'\s#]+)', stripped)
                    if m:
                        current_id = m.group(1)
                elif stripped.startswith('abstract:') and 'true' in stripped:
                    current_abstract = True

            # Don't forget last entity in file
            if current_type == 'entity' and current_id and not current_abstract:
                if not current_id.startswith('Base'):
                    ids.add(current_id)

    return sorted(ids)


def generate_map_yaml(ids, spacing=DEFAULT_SPACING, items_per_row=DEFAULT_ITEMS_PER_ROW,
                      start_x=DEFAULT_START_X, start_y=DEFAULT_START_Y,
                      template_path=None):
    """Generate map YAML by inserting entities into empty.yml template."""
    if template_path and template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = (
            "meta:\n  format: 6\n  postmapinit: false\n"
            "tilemap:\n  0: Space\nentities:\n"
            '- proto: ""\n  entities:\n  - uid: 1\n    components:\n'
            "    - type: MetaData\n    - type: Transform\n    - type: LoadedMap\n...\n"
        )

    uids = re.findall(r'uid: (\d+)', template)
    next_uid = max(int(u) for u in uids) + 1 if uids else 2

    entity_lines = []
    for i, proto_id in enumerate(ids):
        row = i // items_per_row
        col = i % items_per_row
        x = start_x + col * spacing
        y = start_y + row * spacing
        entity_lines.append(f'- proto: {proto_id}')
        entity_lines.append("  entities:")
        entity_lines.append(f"  - uid: {next_uid}")
        entity_lines.append("    components:")
        entity_lines.append("    - type: Transform")
        entity_lines.append(f"      pos: {x},{y}")
        entity_lines.append("      parent: 2")
        next_uid += 1

    entities_block = "\n".join(entity_lines)

    if "...\n" in template:
        result = template.replace("...\n", entities_block + "\n...\n")
    elif template.rstrip().endswith("..."):
        result = template.rstrip()[:-3] + entities_block + "\n...\n"
    else:
        result = template.rstrip() + "\n" + entities_block + "\n"

    # Fix: rename BecomesStation id from "Empty" to "AavikkoShowcase"
    # so it matches the gameMap prototype's station name.
    # Without this, SS14 crashes with "station does not have an associated station config!"
    result = result.replace("id: Empty", "id: AavikkoShowcase")
    result = result.replace('"Empty Debug Map"', '"Aavikko Showcase Map"')

    return result


def generate_map_prototype():
    """Create gameMap prototype for AavikkoShowcase.

    Uses StationJobs with ONLY Passenger — no other jobs.
    ADT has custom jobs (Magistrate, TramDriver, etc.) that cause
    StationJobsSystem.AssignJobs to crash if listed in a custom map
    but not registered in the station's job list.
    """
    return (
        "- type: gameMap\n"
        "  id: AavikkoShowcase\n"
        "  mapName: 'Aavikko Showcase'\n"
        "  mapPath: /Maps/aavikko_showcase.yml\n"
        "  minPlayers: 0\n"
        "  maxPlayers: 1\n"
        "  fallback: false\n"
        "  stations:\n"
        "    AavikkoShowcase:\n"
        "      stationProto: StandardNanotrasenStation\n"
        "      components:\n"
        "        - type: StationNameSetup\n"
        "          mapNameTemplate: 'Aavikko Showcase'\n"
        "        - type: StationJobs\n"
        "          availableJobs:\n"
        "            Passenger: [ -1, -1 ]\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Generate SS14 showcase map with all Aavikko entity prototypes"
    )
    parser.add_argument("--spacing", type=float, default=DEFAULT_SPACING)
    parser.add_argument("--per-row", type=int, default=DEFAULT_ITEMS_PER_ROW)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    build_root = script_dir.parent.parent.parent
    resources_dir = build_root / "00_Aavikko" / "01_Resources"

    patches_dir = resources_dir / "Patches"
    mods_dir = resources_dir / "Mods"

    ids = extract_prototype_ids([patches_dir, mods_dir])
    print(f"Found {len(ids)} spawnable entity prototypes")

    if args.list_only:
        print("\nEntity prototype IDs:")
        for pid in ids:
            print(f"  {pid}")
        return 0

    template = build_root / "Resources" / "Maps" / "Test" / "empty.yml"
    map_yaml = generate_map_yaml(
        ids, spacing=args.spacing, items_per_row=args.per_row,
        template_path=template
    )

    map_path = build_root / "Resources" / "Maps" / "aavikko_showcase.yml"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(map_yaml, encoding="utf-8")
    print(f"Map saved: {map_path}")
    print(f"  {len(ids)} entities, grid {args.per_row}/row, {args.spacing} spacing")

    proto_path = build_root / "Resources" / "Prototypes" / "Maps" / "aavikko_showcase.yml"
    proto_path.parent.mkdir(parents=True, exist_ok=True)
    proto_path.write_text(generate_map_prototype(), encoding="utf-8")
    print(f"  Prototype: {proto_path}")

    print(f"\nTo load: forcemap AavikkoShowcase")
    print(f"To freeze: togglesubscriber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
