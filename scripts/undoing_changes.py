#!/usr/bin/env python3
"""Reverse configured one-to-one replacements in explicit normalisation runs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from normalise_data import (
    RUN_TIMESTAMP_RE,
    SUPPORTED_FORMATS,
    SourceSelection,
    current_timestamp,
    parse_replacement_rules,
    read_simple_yaml,
    replace_by_rules,
    require_list,
    transform_source,
    validate_config,
)


DEFAULT_CONFIG = Path("config/normalisation_spec.yaml")


@dataclass(frozen=True)
class UndoRun:
    run_dir: Path
    data_root: Path
    normalise_timestamp: str
    output_dir: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--run-dir",
        type=Path,
        action="append",
        required=True,
        help="Normalised timestamp directory to undo; may be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts and destinations without writing undone files.",
    )
    return parser.parse_args()


def build_reverse_rules(config: dict[str, object]) -> list[tuple[str, str]]:
    forward: list[tuple[str, str]] = []
    if bool(config.get("replace_sequences", True)):
        forward.extend(
            parse_replacement_rules(require_list(config, "sequence_replacements", []))
        )
    if bool(config.get("map_tones", True)):
        forward.extend(
            parse_replacement_rules(require_list(config, "tone_replacements", []))
        )
    targets = [target for _, target in forward if target]
    if len(targets) != len(set(targets)):
        raise ValueError("Undo rules are ambiguous because replacement targets repeat")
    return [(target, source) for source, target in forward if target]


def describe_run(run_dir: Path, undo_timestamp: str) -> UndoRun:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise ValueError(f"Normalisation run directory does not exist: {run_dir}")
    if run_dir.parent.name.casefold() != "normalised":
        raise ValueError(f"Run directory must be directly below a normalised folder: {run_dir}")
    if not RUN_TIMESTAMP_RE.fullmatch(run_dir.name):
        raise ValueError(f"Run directory name is not a normalisation timestamp: {run_dir.name}")
    data_root = run_dir.parent.parent
    output_dir = data_root / "undone" / f"{run_dir.name}-{undo_timestamp}"
    return UndoRun(run_dir, data_root, run_dir.name, output_dir)


def find_run_sources(run: UndoRun) -> list[SourceSelection]:
    sources: list[SourceSelection] = []
    for path in sorted(item for item in run.run_dir.rglob("*") if item.is_file()):
        source_type = path.suffix.casefold()
        if source_type not in SUPPORTED_FORMATS:
            continue
        sources.append(
            SourceSelection(
                path=path,
                data_root=run.run_dir,
                source_type=source_type,
                selected=True,
            )
        )
    return sources


def run_undo(
    config: dict[str, object],
    run_directories: list[Path],
    *,
    dry_run: bool,
    timestamp: str | None = None,
) -> tuple[list[object], list[UndoRun]]:
    validate_config(config)
    undo_timestamp = timestamp or current_timestamp()
    if not RUN_TIMESTAMP_RE.fullmatch(undo_timestamp):
        raise ValueError(f"Invalid undo timestamp: {undo_timestamp}")
    runs = [describe_run(path, undo_timestamp) for path in run_directories]
    if len({run.run_dir for run in runs}) != len(runs):
        raise ValueError("The same --run-dir was provided more than once")
    existing = [run.output_dir for run in runs if run.output_dir.exists()]
    if existing:
        raise FileExistsError("Undo destination already exists: " + ", ".join(map(str, existing)))

    rules = build_reverse_rules(config)
    transform = lambda text: replace_by_rules(text, rules)
    sources_by_run = [(run, find_run_sources(run)) for run in runs]
    if not any(sources for _, sources in sources_by_run):
        raise ValueError("No supported files were found in the selected normalisation runs")
    # Validate every file and selector before creating any undo destination.
    preview_results: list[object] = []
    for run, sources in sources_by_run:
        for source in sources:
            output_path = run.output_dir / source.path.relative_to(run.run_dir)
            preview_results.append(
                transform_source(source, output_path, config, transform, dry_run=True)
            )
    if dry_run:
        return preview_results, runs

    for run, sources in sources_by_run:
        if sources:
            run.output_dir.mkdir(parents=True, exist_ok=False)

    results: list[object] = []
    for run, sources in sources_by_run:
        for source in sources:
            output_path = run.output_dir / source.path.relative_to(run.run_dir)
            results.append(
                transform_source(source, output_path, config, transform, dry_run=False)
            )
    return results, runs


def main() -> None:
    args = parse_args()
    config = read_simple_yaml(args.config.resolve())
    results, runs = run_undo(
        config,
        args.run_dir,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print("Dry run: no undone files written.")
    print(f"Files processed: {len(results)}")
    print(f"Annotations/cells changed: {sum(result.annotations_changed for result in results)}")
    for run in runs:
        print(f"Undo output for {run.run_dir}: {run.output_dir}")
    print(
        "Only one-to-one replacements were reversed; removals, whitespace changes, "
        "bracket removal, and case conversion were not reconstructed."
    )


if __name__ == "__main__":
    main()
