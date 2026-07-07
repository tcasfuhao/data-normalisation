#!/usr/bin/env python3
"""Normalise configured transcription data into copied EAF files."""

from __future__ import annotations

import argparse
import csv
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("config/normalization_spec.yaml")


@dataclass(frozen=True)
class TranscriptionPath:
    path: Path
    source_type: str
    selected: bool
    warning: str = ""


@dataclass
class NormalisationResult:
    source_path: Path
    output_path: Path | None
    source_type: str
    annotations_seen: int
    annotations_changed: int
    empty_after_normalisation_count: int
    warnings: list[str]
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
        help="Print counts and sample transformations without writing outputs.",
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


def format_config_template(value: str | Path, config: dict[str, Any]) -> str:
    template_values = {key: str(config_value) for key, config_value in config.items()}
    return str(value).format_map(template_values)


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


def replace_by_rules(text: str, rules: list[tuple[str, str]]) -> str:
    for source, target in sorted(rules, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, target)
    return text


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def looks_like_time(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def read_txt_values(path: Path) -> list[str]:
    values: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3 and looks_like_time(parts[0]) and looks_like_time(parts[1]):
            text = "\t".join(parts[2:]).strip()
        elif len(parts) >= 4 and looks_like_time(parts[1]) and looks_like_time(parts[2]):
            text = "\t".join(parts[3:]).strip()
        else:
            text = line
        if text:
            values.append(text)
    return values


def find_transcription_paths(data_root: Path, patterns: list[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in data_root.glob(pattern) if path.is_file())
        if "**" not in pattern:
            paths.update(path for path in data_root.rglob(pattern) if path.is_file())
    return sorted(paths)


def read_data_roots(config: dict[str, Any], project_dir: Path) -> list[Path]:
    if "data_roots" in config:
        raw_roots = require_list(config, "data_roots", [])
    elif "data_root" in config:
        raw_roots = [str(config["data_root"])]
    else:
        raise ValueError("Config must define data_root or data_roots")
    if not raw_roots:
        raise ValueError("Config data_roots must not be empty")
    return [resolve_path(raw_root, project_dir) for raw_root in raw_roots]


def matching_data_root(path: Path, data_roots: list[Path]) -> Path:
    matches: list[Path] = []
    for data_root in data_roots:
        try:
            path.relative_to(data_root)
        except ValueError:
            continue
        matches.append(data_root)
    if not matches:
        raise ValueError(f"{path} is not under any configured data root")
    return max(matches, key=lambda item: len(item.parts))


def relative_to_data_roots(path: Path, data_roots: list[Path]) -> Path:
    return path.relative_to(matching_data_root(path, data_roots))


def recording_key(path: Path) -> str:
    stem = path.stem
    if path.suffix.lower() == ".txt":
        return re.sub(r"-[A-Z]$", "", stem)
    return stem


def choose_transcription_paths(paths: list[Path], prefer_eaf: bool) -> list[TranscriptionPath]:
    if not prefer_eaf:
        return [
            TranscriptionPath(path=path, source_type=path.suffix.lower().lstrip("."), selected=True)
            for path in paths
        ]

    grouped: dict[str, list[Path]] = {}
    for path in paths:
        grouped.setdefault(recording_key(path), []).append(path)

    chosen: list[TranscriptionPath] = []
    for group_paths in grouped.values():
        eaf_paths = sorted(path for path in group_paths if path.suffix.lower() == ".eaf")
        txt_paths = sorted(path for path in group_paths if path.suffix.lower() == ".txt")
        if eaf_paths:
            chosen.extend(
                TranscriptionPath(path=path, source_type="eaf", selected=True)
                for path in eaf_paths
            )
            chosen.extend(
                TranscriptionPath(
                    path=path,
                    source_type="txt",
                    selected=False,
                    warning="matching EAF selected; TXT skipped",
                )
                for path in txt_paths
            )
        else:
            chosen.extend(
                TranscriptionPath(path=path, source_type="txt", selected=True)
                for path in txt_paths
            )
    return sorted(chosen, key=lambda item: item.path)


def remove_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(
        char for char in decomposed if unicodedata.category(char) != "Mn"
    )
    return unicodedata.normalize("NFC", stripped)


def normalise_text(text: str, policy: dict[str, Any]) -> str:
    for step in policy["normalization_steps"]:
        if step == "remove_bracketed_text":
            if policy["remove_bracketed_text"]:
                text = re.sub(r"\[[^\]]*\]", " ", text)
                text = re.sub(r"\([^)]*\)", " ", text)
        elif step == "replace_sequences":
            if policy["replace_sequences"]:
                text = replace_by_rules(text, policy["sequence_replacements"])
        elif step == "lowercase_text":
            if policy["lowercase_text"]:
                text = text.lower()
        elif step in {"map_tones"}:
            if not policy["map_tones"]:
                text = replace_by_rules(text, policy["tone_replacements"])
        elif step in {"remove_punctuation", "remove_configured_punctuation"}:
            remove_chars = set(policy["remove_chars"])
            if policy["remove_punctuation"]:
                text = "".join(" " if char in remove_chars else char for char in text)
        elif step in {"remove_hash", "remove_hash_markers"}:
            text = text.replace("#", " ") if policy["remove_hash"] else text
        elif step == "collapse_whitespace":
            text = re.sub(r"\s+", " ", text).strip() if policy["collapse_whitespace"] else text
        elif step == "strip_spaces":
            text = re.sub(r"\s+", "", text) if policy["strip_spaces"] else text
        elif step == "remove_diacritics":
            text = remove_diacritics(text) if policy["remove_diacritics"] else text
        else:
            raise ValueError(f"Unknown normalization step: {step}")
    return text


def output_path_for(source_path: Path, data_root: Path, eaf_output_dir: Path) -> Path:
    relative = source_path.relative_to(data_root)
    return eaf_output_dir / relative.with_suffix(".eaf")


def skipped_txt_result(source_path: Path, warning: str) -> NormalisationResult:
    return NormalisationResult(
        source_path=source_path,
        output_path=None,
        source_type="txt",
        annotations_seen=0,
        annotations_changed=0,
        empty_after_normalisation_count=0,
        warnings=[warning],
        samples=[],
    )


def normalise_eaf(
    source_path: Path,
    output_path: Path,
    data_root: Path,
    policy: dict[str, Any],
    *,
    dry_run: bool,
) -> NormalisationResult:
    warnings: list[str] = []
    samples: list[tuple[str, str]] = []
    tree = ET.parse(source_path)
    root = tree.getroot()

    annotations_seen = 0
    annotations_changed = 0
    empty_after = 0

    for element in root.iter():
        if strip_namespace(element.tag) != "ANNOTATION_VALUE":
            continue
        annotations_seen += 1
        original = element.text or ""
        if original == "":
            continue
        normalised = normalise_text(original, policy)
        if normalised == "":
            empty_after += 1
        if normalised != original:
            annotations_changed += 1
            if len(samples) < 10:
                samples.append((original, normalised))
            if not dry_run:
                element.text = normalised

    if not dry_run:
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(output_path, encoding="UTF-8", xml_declaration=True)

    return NormalisationResult(
        source_path=source_path,
        output_path=output_path,
        source_type="eaf",
        annotations_seen=annotations_seen,
        annotations_changed=annotations_changed,
        empty_after_normalisation_count=empty_after,
        warnings=warnings,
        samples=samples,
    )


def summarise_txt(
    source_path: Path,
    policy: dict[str, Any],
    warning: str,
) -> NormalisationResult:
    values = read_txt_values(source_path)
    annotations_changed = 0
    empty_after = 0
    samples: list[tuple[str, str]] = []
    for value in values:
        normalised = normalise_text(value, policy)
        if normalised == "":
            empty_after += 1
        if normalised != value:
            annotations_changed += 1
            if len(samples) < 10:
                samples.append((value, normalised))
    warnings = [warning] if warning else ["TXT fallback summarised only; no EAF output written"]
    return NormalisationResult(
        source_path=source_path,
        output_path=None,
        source_type="txt",
        annotations_seen=len(values),
        annotations_changed=annotations_changed,
        empty_after_normalisation_count=empty_after,
        warnings=warnings,
        samples=samples,
    )


def write_summary_csv(
    path: Path,
    results: list[NormalisationResult],
    data_roots: list[Path],
    project_dir: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(
            file_obj,
            fieldnames=[
                "source_path",
                "output_path",
                "source_type",
                "annotations_seen",
                "annotations_changed",
                "empty_after_normalisation_count",
                "warnings",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "source_path": relative_to_data_roots(
                        result.source_path, data_roots
                    ).as_posix(),
                    "output_path": (
                        result.output_path.relative_to(project_dir).as_posix()
                        if result.output_path
                        else ""
                    ),
                    "source_type": result.source_type,
                    "annotations_seen": result.annotations_seen,
                    "annotations_changed": result.annotations_changed,
                    "empty_after_normalisation_count": result.empty_after_normalisation_count,
                    "warnings": "; ".join(result.warnings),
                }
            )


def build_policy(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "normalization_steps": require_list(config, "normalization_steps", []),
        "sequence_replacements": parse_replacement_rules(
            require_list(config, "sequence_replacements", [])
        ),
        "tone_replacements": parse_replacement_rules(require_list(config, "tone_replacements", [])),
        "remove_bracketed_text": bool(config.get("remove_bracketed_text", True)),
        "replace_sequences": bool(config.get("replace_sequences", True)),
        "map_tones": bool(config.get("map_tones", True)),
        "lowercase_text": bool(config.get("lowercase_text", False)),
        "remove_chars": require_list(config, "remove_chars", []),
        "remove_hash": bool(config.get("remove_hash", True)),
        "remove_punctuation": bool(config.get("remove_punctuation", True)),
        "collapse_whitespace": bool(config.get("collapse_whitespace", True)),
        "strip_spaces": bool(config.get("strip_spaces", True)),
        "remove_diacritics": bool(config.get("remove_diacritics", False)),
    }


def print_dry_run(results: list[NormalisationResult]) -> None:
    total_seen = sum(result.annotations_seen for result in results)
    total_changed = sum(result.annotations_changed for result in results)
    total_empty = sum(result.empty_after_normalisation_count for result in results)
    print("Dry run: no files written.")
    print(f"Sources summarised: {len(results)}")
    print(f"Annotations seen: {total_seen}")
    print(f"Annotations changed: {total_changed}")
    print(f"Empty after normalisation: {total_empty}")
    print()
    for result in results:
        print(
            f"{result.source_type}: {result.source_path} "
            f"seen={result.annotations_seen} changed={result.annotations_changed}"
        )
        for original, normalised in result.samples[:3]:
            print(f"  {original} -> {normalised}")
        for warning in result.warnings:
            print(f"  warning: {warning}")


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    project_dir = config_path.parent.parent
    config = read_simple_yaml(config_path)
    policy = build_policy(config)

    data_roots = read_data_roots(config, project_dir)
    transcription_globs = require_list(config, "transcription_globs", ["*.eaf", "*.txt"])
    prefer_eaf = bool(config.get("prefer_eaf", True))
    output_dir = resolve_path(
        format_config_template(config.get("output_dir", "output"), config),
        project_dir,
    )
    eaf_output_dir = resolve_path(
        format_config_template(config.get("eaf_output_dir", "output/eaf"), config),
        project_dir,
    )
    summary_path = resolve_path(
        format_config_template(
            config.get("summary_csv_path", "output/logs/normalisation_summary.csv"),
            config,
        ),
        project_dir,
    )

    paths = [
        path
        for data_root in data_roots
        for path in find_transcription_paths(data_root, transcription_globs)
        if path.suffix.lower() in {".eaf", ".txt"}
    ]
    transcription_paths = choose_transcription_paths(paths, prefer_eaf)

    results: list[NormalisationResult] = []
    for transcription_path in transcription_paths:
        if transcription_path.source_type == "eaf" and transcription_path.selected:
            data_root = matching_data_root(transcription_path.path, data_roots)
            output_path = output_path_for(transcription_path.path, data_root, eaf_output_dir)
            results.append(
                normalise_eaf(
                    transcription_path.path,
                    output_path,
                    data_root,
                    policy,
                    dry_run=args.dry_run,
                )
            )
            continue
        if transcription_path.source_type == "txt" and not transcription_path.selected:
            results.append(
                skipped_txt_result(transcription_path.path, transcription_path.warning)
            )
            continue
        results.append(
            summarise_txt(transcription_path.path, policy, transcription_path.warning)
        )

    if args.dry_run:
        print_dry_run(results)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    write_summary_csv(summary_path, results, data_roots, project_dir)

    eaf_written = sum(1 for result in results if result.output_path)
    eaf_changed = sum(
        result.annotations_changed for result in results if result.output_path
    )
    total_changed = sum(result.annotations_changed for result in results)
    print(f"Normalised EAF files written: {eaf_written}")
    print(f"EAF annotations changed: {eaf_changed}")
    print(f"Total annotations changed including TXT summaries: {total_changed}")
    print(f"Summary CSV: {summary_path}")


if __name__ == "__main__":
    main()
