# World Exporter: behavior and implementation

Describes the exported implementation **0.1.0**. The linked source and metadata define this version; gameplay qualification is a separate requirement.

## Behavior

A standalone standard-library Python companion exports a complete last-saved slot as .msworld.zip without loading the game. configure selects explicit source/output locations; list enumerates valid slots; export snapshots a selected slot and writes a deterministic archive with manifest and payload hashes. inspect reads archive metadata and verify checks payload integrity. Inputs, member names, duplicate entries, symlinks and output/source overlap are validated.

## State and lifecycle

Configuration persists companion source/output choices. Export reads source saves only and retries boundedly when source identities change during capture; it publishes a completed archive without overwriting an existing destination. Slot IDs and complete file/directory identities remain in the manifest. It never combines Steam and Bottles source profiles.

## Integration and configuration

No additional feature-mod runtime dependency is declared. Included shared headers are source reuse, not extra DLL dependencies.

The [module metadata](../source/mods/world-exporter/mod.json) owns the version and declared integration contract. This companion runs separately through its Python entry point; it does not inject a native gameplay DLL.

## Implementation sources

- [world_exporter/__main__.py](../source/mods/world-exporter/world_exporter/__main__.py)
- [world_exporter/archive.py](../source/mods/world-exporter/world_exporter/archive.py)

## Verification boundary

No unsaved-memory capture, import, version conversion or game-side restore. Structural/hash validity does not establish that an exported world loads in another build. Source must be a complete stable saved slot.

`python3 build.py --check` verifies the exported source hashes. It does not execute the mod or qualify the behaviors above. Installation and independent build prerequisites are in the [README](../README.md).
