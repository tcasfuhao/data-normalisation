#!/usr/bin/env python3
"""Reverse configured one-to-one symbol replacements in normalised EAF files."""

from __future__ import annotations

import argparse
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("config/normalization_spec.yaml")


@dataclass
class UndoResult:
    source_path: Path
    output_path: Path
    annotations_seen: int
    annotations_changed: int
    samples: list[tuple[str, str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Normalisation YAML config. Default: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts and sample reversals without writing outputs.",
    )
    return parser.parse_args()


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"null", "None", "~"}:
        return None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None:
            return line[:idx]
    return line


def read_simple_yaml(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line_without_comment = strip_yaml_comment(raw_line).rstrip()
        if not line_without_comment.strip():
            continue
        stripped = line_without_comment.strip()

        if stripped.startswith("- "):
            if current_list_key is None:
                raise ValueError(f"List item without key in {path}: {raw_line}")
            values[current_list_key].append(parse_scalar(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"Unsupported YAML line in {path}: {raw_line}")

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            values[key] = parse_scalar(value)
            current_list_key = None
        else:
            values[key] = []
            current_list_key = key
    return values


def require_list(config: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = config.get(key, default)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a YAML list or string")
    return [str(item) for item in value]


def resolve_path(value: str | Path, base_dir: Path) -> Path:
    raw_value = str(value)
    if raw_value.startswith("~projects/"):
        raw_value = f"~/{raw_value[1:]}"

    path = Path(os.path.expanduser(raw_value))
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def parse_replacement_rules(raw_rules: list[str]) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for raw_rule in raw_rules:
        if "=>" not in raw_rule:
            raise ValueError(f"Replacement rule must use 'source=>target': {raw_rule}")
        source, target = raw_rule.split("=>", 1)
        source = source.strip()
        target = target.strip()
        if not source:
            raise ValueError(f"Replacement rule has empty source: {raw_rule}")
        rules.append((source, target))
    return rules


def reverse_rules(rules: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(target, source) for source, target in rules if target]


def replace_by_rules(text: str, rules: list[tuple[str, str]]) -> str:
    for source, target in sorted(rules, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, target)
    return text


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def undo_eaf(
    source_path: Path,
    output_path: Path,
    rules: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> UndoResult:
    samples: list[tuple[str, str]] = []
    tree = ET.parse(source_path)
    root = tree.getroot()

    annotations_seen = 0
    annotations_changed = 0
    for element in root.iter():
        if strip_namespace(element.tag) != "ANNOTATION_VALUE":
            continue
        annotations_seen += 1
        original = element.text or ""
        if original == "":
            continue
        undone = replace_by_rules(original, rules)
        if undone != original:
            annotations_changed += 1
            if len(samples) < 10:
                samples.append((original, undone))
            if not dry_run:
                element.text = undone

    if not dry_run:
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(output_path, encoding="UTF-8", xml_declaration=True)

    return UndoResult(
        source_path=source_path,
        output_path=output_path,
        annotations_seen=annotations_seen,
        annotations_changed=annotations_changed,
        samples=samples,
    )


def find_eaf_paths(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*.eaf") if path.is_file())


def output_path_for(source_path: Path, input_dir: Path, output_dir: Path) -> Path:
    return output_dir / source_path.relative_to(input_dir)


def print_dry_run(results: list[UndoResult]) -> None:
    print("Dry run: no files written.")
    print(f"EAF files summarised: {len(results)}")
    print(f"Annotations seen: {sum(result.annotations_seen for result in results)}")
    print(f"Annotations changed: {sum(result.annotations_changed for result in results)}")
    print()
    for result in results:
        print(
            f"{result.source_path} "
            f"seen={result.annotations_seen} changed={result.annotations_changed}"
        )
        for original, undone in result.samples[:3]:
            print(f"  {original} -> {undone}")


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    project_dir = config_path.parent.parent
    config = read_simple_yaml(config_path)

    input_dir = resolve_path(config.get("eaf_output_dir", "output/eaf"), project_dir)
    output_dir = resolve_path(config.get("undo_eaf_output_dir", "output/undo/eaf"), project_dir)
    rules = reverse_rules(
        parse_replacement_rules(require_list(config, "sequence_replacements", []))
    )

    results: list[UndoResult] = []
    for source_path in find_eaf_paths(input_dir):
        results.append(
            undo_eaf(
                source_path,
                output_path_for(source_path, input_dir, output_dir),
                rules,
                dry_run=args.dry_run,
            )
        )

    if args.dry_run:
        print_dry_run(results)
        return

    print(f"Undo EAF files written: {len(results)}")
    print(f"Annotations changed: {sum(result.annotations_changed for result in results)}")
    print(f"Undo output: {output_dir}")
    print("Only one-to-one replacements were reversed; removed text was not reconstructed.")


if __name__ == "__main__":
    main()
