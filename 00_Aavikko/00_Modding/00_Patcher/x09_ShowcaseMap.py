#!/usr/bin/env python3
"""
generate_showcase_map.py — Generate an SS14 map that displays ALL Aavikko
prototype IDs as spawned entities in a grid.

For entity prototypes: spawn directly with proto: <id>
For non-entity prototypes with sprites (jobIcon, etc.): spawn BaseItem with
  overridden Sprite component using the icon's sprite path + state.

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

DEFAULT_SPACING = 1.5
DEFAULT_ITEMS_PER_ROW = 20
DEFAULT_START_X = 2.5
DEFAULT_START_Y = 2.5

# Non-entity types that have "icon:" with sprite info
# We can spawn these as BaseItem with overridden Sprite component
SPRITE_TYPES = {'jobIcon', 'statusIcon'}


def parse_prototypes(yml_dirs):
    """Parse all .yml files and return list of (type, id, sprite_path, sprite_state, abstract).

    For entity types: sprite_path/state are None (use entity's own sprite).
    For jobIcon/statusIcon: extract icon.sprite and icon.state.
    """
    prototypes = []
    seen_ids = set()

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

            current_type = None
            current_id = None
            current_abstract = False
            current_sprite = None
            current_state = None

            for line in content.split('\n'):
                stripped = line.strip()
                if stripped.startswith('- type:'):
                    # Save previous prototype
                    if current_id and current_id not in seen_ids:
                        if not current_abstract and not current_id.startswith('Base'):
                            prototypes.append((current_type, current_id,
                                             current_sprite, current_state))
                            seen_ids.add(current_id)
                    # Start new block
                    m = re.match(r'- type:\s*["\']?([^"\'\s#]+)', stripped)
                    current_type = m.group(1) if m else None
                    current_id = None
                    current_abstract = False
                    current_sprite = None
                    current_state = None
                elif stripped.startswith('id:') and current_type:
                    m = re.match(r'id:\s*["\']?([^"\'\s#]+)', stripped)
                    if m:
                        current_id = m.group(1)
                elif stripped.startswith('abstract:') and 'true' in stripped:
                    current_abstract = True
                elif stripped.startswith('sprite:') and current_type in SPRITE_TYPES:
                    m = re.match(r'sprite:\s*(.+)', stripped)
                    if m:
                        current_sprite = m.group(1).strip().rstrip('#').strip()
                elif stripped.startswith('state:') and current_type in SPRITE_TYPES:
                    m = re.match(r'state:\s*["\']?([^"\'\s#]+)', stripped)
                    if m:
                        current_state = m.group(1)

            # Don't forget last prototype in file
            if current_id and current_id not in seen_ids:
                if not current_abstract and not current_id.startswith('Base'):
                    prototypes.append((current_type, current_id,
                                     current_sprite, current_state))
                    seen_ids.add(current_id)

    return sorted(prototypes, key=lambda x: x[1])


def generate_map_yaml(prototypes, spacing=DEFAULT_SPACING,
                      items_per_row=DEFAULT_ITEMS_PER_ROW,
                      start_x=DEFAULT_START_X, start_y=DEFAULT_START_Y,
                      template_path=None):
    """Generate map YAML.

    For entity prototypes: spawn directly.
    For jobIcon/statusIcon: spawn BaseItem with overridden Sprite.
    """
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
    for i, (proto_type, proto_id, sprite_path, sprite_state) in enumerate(prototypes):
        row = i // items_per_row
        col = i % items_per_row
        x = start_x + col * spacing
        y = start_y + row * spacing

        if proto_type == 'entity':
            # Direct spawn
            entity_lines.append(f'- proto: {proto_id}')
            entity_lines.append("  entities:")
            entity_lines.append(f"  - uid: {next_uid}")
            entity_lines.append("    components:")
            entity_lines.append("    - type: Transform")
            entity_lines.append(f"      pos: {x},{y}")
            entity_lines.append("      parent: 2")
        elif proto_type in SPRITE_TYPES and sprite_path:
            # Spawn BaseItem with overridden Sprite
            entity_lines.append('- proto: BaseItem')
            entity_lines.append("  entities:")
            entity_lines.append(f"  - uid: {next_uid}")
            entity_lines.append("    components:")
            entity_lines.append("    - type: MetaData")
            entity_lines.append(f'      name: "{proto_id}"')
            entity_lines.append("    - type: Transform")
            entity_lines.append(f"      pos: {x},{y}")
            entity_lines.append("      parent: 2")
            entity_lines.append("    - type: Sprite")
            entity_lines.append(f"      sprite: {sprite_path}")
            if sprite_state:
                entity_lines.append(f"      state: {sprite_state}")
        else:
            # Skip non-spawnable types without sprite info
            continue

        next_uid += 1

    entities_block = "\n".join(entity_lines)

    if "...\n" in template:
        result = template.replace("...\n", entities_block + "\n...\n")
    elif template.rstrip().endswith("..."):
        result = template.rstrip()[:-3] + entities_block + "\n...\n"
    else:
        result = template.rstrip() + "\n" + entities_block + "\n"

    # Rename BecomesStation id to match gameMap prototype
    result = result.replace("id: Empty", "id: AavikkoShowcase")
    result = result.replace('"Empty Debug Map"', '"Aavikko Showcase Map"')

    return result


def generate_map_prototype():
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
        description="Generate SS14 showcase map with all Aavikko prototypes"
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

    prototypes = parse_prototypes([patches_dir, mods_dir])

    entities = [p for p in prototypes if p[0] == 'entity']
    icons = [p for p in prototypes if p[0] in SPRITE_TYPES and p[2]]
    skipped = [p for p in prototypes if p[0] != 'entity' and p[0] not in SPRITE_TYPES]

    print(f"Found {len(prototypes)} total prototypes:")
    print(f"  {len(entities)} entity (direct spawn)")
    print(f"  {len(icons)} jobIcon/statusIcon (sprite override)")
    print(f"  {len(skipped)} other (skipped)")

    if args.list_only:
        print("\nEntity prototypes:")
        for _, pid, _, _ in entities:
            print(f"  [entity] {pid}")
        print("\nSprite prototypes:")
        for ptype, pid, sprite, state in icons:
            print(f"  [{ptype}] {pid} → {sprite}:{state}")
        return 0

    template = build_root / "Resources" / "Maps" / "Test" / "empty.yml"
    map_yaml = generate_map_yaml(
        prototypes, spacing=args.spacing, items_per_row=args.per_row,
        template_path=template
    )

    spawnable = len(entities) + len(icons)
    map_path = build_root / "Resources" / "Maps" / "aavikko_showcase.yml"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(map_yaml, encoding="utf-8")
    print(f"\nMap saved: {map_path}")
    print(f"  {spawnable} entities ({len(entities)} direct + {len(icons)} sprite override)")
    print(f"  Grid: {args.per_row}/row, {args.spacing} spacing")

    proto_path = build_root / "Resources" / "Prototypes" / "Maps" / "aavikko_showcase.yml"
    proto_path.parent.mkdir(parents=True, exist_ok=True)
    proto_path.write_text(generate_map_prototype(), encoding="utf-8")
    print(f"  Prototype: {proto_path}")

    print(f"\nTo load: forcemap AavikkoShowcase")
    print(f"To freeze: togglesubscriber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
