#!/usr/bin/env python3
"""Normalise configured transcription data into timestamped source-side runs."""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_CONFIG = Path("config/normalisation_spec.yaml")
SUPPORTED_FORMATS = {".eaf", ".textgrid", ".csv", ".tsv", ".txt"}
RESERVED_DIRECTORIES = {"normalised", "undone"}
OBSOLETE_KEYS = {"prefer_eaf", "output_dir", "eaf_output_dir", "summary_csv_path"}
RUN_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
TEXTGRID_ITEM_RE = re.compile(r"^\s*item\s+\[\d+\]:\s*$")
TEXTGRID_CLASS_RE = re.compile(r'^\s*class\s*=\s*"((?:""|[^"])*)"\s*$')
TEXTGRID_NAME_RE = re.compile(r'^\s*name\s*=\s*"((?:""|[^"])*)"\s*$')
TEXTGRID_VALUE_RE = re.compile(
    r'^(\s*(?:text|mark)\s*=\s*")((?:""|[^"])*)("\s*)(\r?\n)?$'
)


@dataclass(frozen=True)
class SourceSelection:
    path: Path
    data_root: Path
    source_type: str
    selected: bool
    warning: str = ""


@dataclass
class NormalisationResult:
    source_path: Path
    data_root: Path
    output_path: Path | None
    run_dir: Path | None
    source_type: str
    selected: bool
    selector: str
    annotations_seen: int
    annotations_changed: int
    empty_after_normalisation_count: int
    warnings: list[str]
    samples: list[tuple[str, str]]


@dataclass(frozen=True)
class EncodedText:
    text: str
    encoding: str
    bom: bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts and destinations without writing data or logs.",
    )
    return parser.parse_args()


def parse_inline_list(value: str) -> list[Any]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: list[Any] = []
    start = 0
    quote: str | None = None
    for index, char in enumerate(inner):
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        elif char == "," and quote is None:
            items.append(parse_scalar(inner[start:index]))
            start = index + 1
    if quote is not None:
        raise ValueError(f"Unterminated quote in inline list: {value}")
    items.append(parse_scalar(inner[start:]))
    return items


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return parse_inline_list(value)
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
    for index, char in enumerate(line):
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
            return line[:index]
    return line


def read_simple_yaml(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = strip_yaml_comment(raw_line).strip()
        if not stripped:
            continue
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


def optional_list(config: dict[str, Any], key: str) -> list[str] | None:
    value = config.get(key)
    if value is None or isinstance(value, bool):
        return None
    return require_list(config, key, [])


def configured_selector(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        selectors = [value.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        selectors = [item.strip() for item in value]
    else:
        raise ValueError(f"{key} must be a name, number, 'all', or a YAML list")
    if not selectors or any(not item for item in selectors):
        raise ValueError(f"{key} must not be empty")
    if len(selectors) > 1 and any(item.casefold() == "all" for item in selectors):
        raise ValueError(f"{key}: 'all' must be used by itself")
    folded = [item.casefold() for item in selectors]
    if len(folded) != len(set(folded)):
        raise ValueError(f"{key} contains duplicate selectors")
    return selectors


def selector_text(selectors: list[str]) -> str:
    return selectors[0] if len(selectors) == 1 else "[" + ", ".join(selectors) + "]"


def validate_config(config: dict[str, Any]) -> None:
    obsolete = sorted(OBSOLETE_KEYS.intersection(config))
    if obsolete:
        names = ", ".join(obsolete)
        raise ValueError(
            f"Obsolete config key(s): {names}. Use prefer, logs_dir, and the "
            "timestamped <data_root>/normalised layout instead."
        )


def resolve_path(value: str | Path, base_dir: Path) -> Path:
    raw_value = str(value)
    if raw_value.startswith("~projects/"):
        raw_value = f"~/{raw_value[1:]}"
    path = Path(os.path.expanduser(raw_value))
    return path if path.is_absolute() else (base_dir / path).resolve()


def read_data_roots(config: dict[str, Any], project_dir: Path) -> list[Path]:
    if "data_roots" in config:
        raw_roots = require_list(config, "data_roots", [])
    elif "data_root" in config:
        raw_roots = [str(config["data_root"])]
    else:
        raise ValueError("Config must define data_root or data_roots")
    if not raw_roots:
        raise ValueError("Config data_roots must not be empty")
    roots = [resolve_path(value, project_dir) for value in raw_roots]
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Configured data root is not a directory: {root}")
    return roots


def matching_data_root(path: Path, data_roots: list[Path]) -> Path:
    matches = [root for root in data_roots if path == root or root in path.parents]
    if not matches:
        raise ValueError(f"{path} is not under any configured data root")
    return max(matches, key=lambda item: len(item.parts))


def canonical_format(value: str) -> str:
    value = value.strip()
    if not value.startswith("."):
        value = "." + value
    folded = value.casefold()
    if folded not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported preferred format {value!r}; supported: {supported}")
    return folded


def preferred_formats(config: dict[str, Any]) -> set[str]:
    raw = configured_selector(config.get("prefer", []), "prefer")
    formats = [canonical_format(item) for item in raw]
    if len(formats) != len(set(formats)):
        raise ValueError("prefer contains duplicate formats")
    return set(formats)


def is_generated_path(path: Path, data_root: Path) -> bool:
    relative = path.relative_to(data_root)
    return any(part.casefold() in RESERVED_DIRECTORIES for part in relative.parts[:-1])


def discover_sources(
    data_roots: list[Path], patterns: list[str], selected_formats: set[str]
) -> list[SourceSelection]:
    discovered: set[Path] = set()
    for root in data_roots:
        for pattern in patterns:
            discovered.update(path for path in root.glob(pattern) if path.is_file())
            if "**" not in pattern:
                discovered.update(path for path in root.rglob(pattern) if path.is_file())

    selections: list[SourceSelection] = []
    for path in sorted(discovered):
        data_root = matching_data_root(path, data_roots)
        if is_generated_path(path, data_root):
            continue
        source_type = path.suffix.casefold()
        if source_type not in SUPPORTED_FORMATS:
            continue
        selected = source_type in selected_formats
        selections.append(
            SourceSelection(
                path=path,
                data_root=data_root,
                source_type=source_type,
                selected=selected,
                warning="" if selected else "format not selected by prefer",
            )
        )
    return selections


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


def remove_balanced_spans(text: str, opener: str, closer: str) -> str:
    """Replace complete, possibly nested bracketed spans with one space.

    An unmatched opening or closing bracket is retained so malformed input does
    not cause the surrounding transcription to be deleted accidentally.
    """
    parts: list[str] = []
    cursor = 0
    span_start: int | None = None
    depth = 0

    for index, char in enumerate(text):
        if char == opener:
            if depth == 0:
                parts.append(text[cursor:index])
                span_start = index
            depth += 1
        elif char == closer and depth > 0:
            depth -= 1
            if depth == 0:
                parts.append(" ")
                cursor = index + 1
                span_start = None

    if depth > 0:
        parts.append(text[span_start:])
    else:
        parts.append(text[cursor:])
    return "".join(parts)


def remove_balanced_bracketed_text(text: str) -> str:
    for opener, closer in (("[", "]"), ("(", ")"), ("{", "}"), ("<", ">")):
        text = remove_balanced_spans(text, opener, closer)
    return text


def build_policy(config: dict[str, Any]) -> dict[str, Any]:
    chars_to_remove = optional_list(config, "chars_to_remove")
    if chars_to_remove is None:
        chars_to_remove = optional_list(config, "remove_chars") or []
    normalisation_steps = require_list(config, "normalisation_steps", [])
    if not normalisation_steps:
        normalisation_steps = require_list(config, "normalization_steps", [])
    drop_sentence_if_contains = require_list(config, "drop_sentence_if_contains", [])
    if any(not value for value in drop_sentence_if_contains):
        raise ValueError("drop_sentence_if_contains cannot contain an empty value")
    return {
        "normalisation_steps": normalisation_steps,
        "sequence_replacements": parse_replacement_rules(
            require_list(config, "sequence_replacements", [])
        ),
        "tone_replacements": parse_replacement_rules(
            require_list(config, "tone_replacements", [])
        ),
        "remove_bracketed_text": bool(config.get("remove_bracketed_text", True)),
        "replace_sequences": bool(config.get("replace_sequences", True)),
        "map_tones": bool(config.get("map_tones", True)),
        "lowercase_text": bool(config.get("lowercase_text", False)),
        "remove_chars": bool(config.get("remove_chars", True)),
        "chars_to_remove": chars_to_remove,
        "drop_sentence_if_contains": drop_sentence_if_contains,
        "collapse_whitespace": bool(config.get("collapse_whitespace", True)),
        "strip_spaces": bool(config.get("strip_spaces", True)),
    }


def normalise_text(text: str, policy: dict[str, Any]) -> str:
    for step in policy["normalisation_steps"]:
        if step == "remove_bracketed_text":
            if policy["remove_bracketed_text"]:
                text = remove_balanced_bracketed_text(text)
        elif step == "remove_chars":
            if policy["remove_chars"]:
                removed = set(policy["chars_to_remove"])
                text = "".join("" if char in removed else char for char in text)
        elif step == "drop_sentence_if_contains":
            if any(value in text for value in policy["drop_sentence_if_contains"]):
                return ""
        elif step == "replace_sequences":
            if policy["replace_sequences"]:
                text = replace_by_rules(text, policy["sequence_replacements"])
        elif step == "lowercase_text":
            if policy["lowercase_text"]:
                text = text.lower()
        elif step == "map_tones":
            if policy["map_tones"]:
                text = replace_by_rules(text, policy["tone_replacements"])
        elif step == "collapse_whitespace":
            if policy["collapse_whitespace"]:
                text = re.sub(r"\s+", " ", text).strip()
        elif step == "strip_spaces":
            if policy["strip_spaces"]:
                text = re.sub(r"\s+", "", text)
        else:
            raise ValueError(f"Unknown normalisation step: {step}")
    return text


def strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def select_names(
    available: list[str], selectors: list[str], path: Path, kind: str
) -> list[str]:
    if selectors[0].casefold() == "all":
        return available
    selected: set[str] = set()
    for requested in selectors:
        matches = [name for name in available if name.casefold() == requested.casefold()]
        if not matches:
            names = ", ".join(repr(name) for name in available) or "<none>"
            raise ValueError(
                f"{path}: requested {kind} {requested!r} was not found; available: {names}"
            )
        if len(matches) > 1:
            raise ValueError(f"{path}: requested {kind} {requested!r} is ambiguous")
        selected.add(matches[0])
    return [name for name in available if name in selected]


def read_encoded_text(path: Path) -> EncodedText:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return EncodedText(raw[2:].decode("utf-16-le"), "utf-16-le", b"\xff\xfe")
    if raw.startswith(b"\xfe\xff"):
        return EncodedText(raw[2:].decode("utf-16-be"), "utf-16-be", b"\xfe\xff")
    if raw.startswith(b"\xef\xbb\xbf"):
        return EncodedText(raw[3:].decode("utf-8"), "utf-8", b"\xef\xbb\xbf")
    return EncodedText(raw.decode("utf-8"), "utf-8", b"")


def write_encoded_text(path: Path, content: str, encoded: EncodedText) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded.bom + content.encode(encoded.encoding))


def transform_eaf(
    source_path: Path,
    output_path: Path,
    selectors: list[str],
    transform: Callable[[str], str],
    *,
    dry_run: bool,
) -> tuple[int, int, int, list[tuple[str, str]]]:
    tree = ET.parse(source_path)
    root = tree.getroot()
    tiers = [element for element in root.iter() if strip_namespace(element.tag) == "TIER"]
    tier_ids = [tier.get("TIER_ID", "") for tier in tiers]
    chosen = set(select_names(tier_ids, selectors, source_path, "EAF tier"))
    seen = changed = empty = 0
    samples: list[tuple[str, str]] = []
    for tier in tiers:
        if tier.get("TIER_ID", "") not in chosen:
            continue
        for element in tier.iter():
            if strip_namespace(element.tag) != "ANNOTATION_VALUE":
                continue
            seen += 1
            original = element.text or ""
            updated = transform(original) if original else original
            if original and not updated:
                empty += 1
            if updated != original:
                changed += 1
                if len(samples) < 10:
                    samples.append((original, updated))
                element.text = updated
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
        tree.write(output_path, encoding="UTF-8", xml_declaration=True)
    return seen, changed, empty, samples


def unescape_textgrid(value: str) -> str:
    return value.replace('""', '"')


def transform_textgrid(
    source_path: Path,
    output_path: Path,
    selectors: list[str],
    transform: Callable[[str], str],
    *,
    dry_run: bool,
) -> tuple[int, int, int, list[tuple[str, str]]]:
    encoded = read_encoded_text(source_path)
    lines = encoded.text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if TEXTGRID_ITEM_RE.match(line.rstrip("\r\n"))]
    if not starts:
        raise ValueError(f"{source_path}: expected Praat long text-format tier blocks")
    tiers: list[tuple[str, int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        tier_class = name = ""
        for line in lines[start:end]:
            bare = line.rstrip("\r\n")
            if not tier_class:
                match = TEXTGRID_CLASS_RE.match(bare)
                if match:
                    tier_class = unescape_textgrid(match.group(1))
            if not name:
                match = TEXTGRID_NAME_RE.match(bare)
                if match:
                    name = unescape_textgrid(match.group(1))
        if tier_class not in {"IntervalTier", "TextTier"} or not name:
            raise ValueError(f"{source_path}: malformed TextGrid tier at line {start + 1}")
        tiers.append((name, start, end))
    chosen = set(select_names([name for name, _, _ in tiers], selectors, source_path, "TextGrid tier"))
    seen = changed = empty = 0
    samples: list[tuple[str, str]] = []
    for name, start, end in tiers:
        if name not in chosen:
            continue
        for index in range(start, end):
            match = TEXTGRID_VALUE_RE.match(lines[index])
            if not match:
                continue
            seen += 1
            original = unescape_textgrid(match.group(2))
            updated = transform(original) if original else original
            if original and not updated:
                empty += 1
            if updated != original:
                changed += 1
                if len(samples) < 10:
                    samples.append((original, updated))
                escaped = updated.replace('"', '""')
                lines[index] = match.group(1) + escaped + match.group(3) + (match.group(4) or "")
    if not dry_run:
        write_encoded_text(output_path, "".join(lines), encoded)
    return seen, changed, empty, samples


def table_settings(config: dict[str, Any], source_type: str) -> tuple[str, bool, list[str]]:
    name = source_type.lstrip(".")
    column_key = f"{name}_text_column"
    if column_key not in config:
        raise ValueError(f"{column_key} must be configured when selecting {source_type}")
    selectors = configured_selector(config[column_key], column_key)
    has_header = bool(config.get(f"{name}_has_header", source_type == ".csv"))
    if source_type == ".csv":
        delimiter = ","
    elif source_type == ".tsv":
        delimiter = "\t"
    else:
        raw_delimiter = str(config.get("txt_delimiter", "")).strip()
        delimiter = {"tab": "\t", "comma": ","}.get(raw_delimiter.casefold(), raw_delimiter)
        if len(delimiter) != 1:
            raise ValueError("txt_delimiter must be 'tab', 'comma', or one character")
    return delimiter, has_header, selectors


def select_column_indices(
    selectors: list[str], header: list[str] | None, width: int, path: Path
) -> list[int]:
    if selectors[0].casefold() == "all":
        return list(range(width))
    indices: set[int] = set()
    for selector in selectors:
        if selector.isdecimal():
            index = int(selector) - 1
            if index < 0 or index >= width:
                raise ValueError(f"{path}: column {selector} is outside 1..{width}")
            indices.add(index)
            continue
        if header is None:
            raise ValueError(f"{path}: headerless tables require numeric column selectors")
        matches = [index for index, name in enumerate(header) if name.casefold() == selector.casefold()]
        if not matches:
            available = ", ".join(repr(name) for name in header) or "<none>"
            raise ValueError(f"{path}: column {selector!r} not found; available: {available}")
        if len(matches) > 1:
            raise ValueError(f"{path}: column {selector!r} is ambiguous")
        indices.add(matches[0])
    return sorted(indices)


def transform_table(
    source_path: Path,
    output_path: Path,
    delimiter: str,
    has_header: bool,
    selectors: list[str],
    transform: Callable[[str], str],
    *,
    dry_run: bool,
) -> tuple[int, int, int, list[tuple[str, str]]]:
    encoded = read_encoded_text(source_path)
    rows = list(csv.reader(io.StringIO(encoded.text, newline=""), delimiter=delimiter))
    if not rows:
        raise ValueError(f"{source_path}: table is empty")
    width = len(rows[0])
    header = rows[0] if has_header else None
    indices = select_column_indices(selectors, header, width, source_path)
    first_data_row = 1 if has_header else 0
    seen = changed = empty = 0
    samples: list[tuple[str, str]] = []
    for row_number, row in enumerate(rows[first_data_row:], start=first_data_row + 1):
        if len(row) != width:
            raise ValueError(
                f"{source_path}:{row_number}: expected {width} columns, found {len(row)}"
            )
        for index in indices:
            seen += 1
            original = row[index]
            updated = transform(original) if original else original
            if original and not updated:
                empty += 1
            if updated != original:
                changed += 1
                if len(samples) < 10:
                    samples.append((original, updated))
                row[index] = updated
    if not dry_run:
        newline = "\r\n" if "\r\n" in encoded.text else "\n"
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=delimiter, lineterminator=newline)
        writer.writerows(rows)
        write_encoded_text(output_path, buffer.getvalue(), encoded)
    return seen, changed, empty, samples


def selectors_for(config: dict[str, Any], source_type: str) -> tuple[list[str], str]:
    if source_type == ".eaf":
        selectors = configured_selector(config.get("eaf_tier", "all"), "eaf_tier")
    elif source_type == ".textgrid":
        selectors = configured_selector(config.get("textgrid_tier", "all"), "textgrid_tier")
    else:
        _, _, selectors = table_settings(config, source_type)
    return selectors, selector_text(selectors)


def transform_source(
    selection: SourceSelection,
    output_path: Path,
    config: dict[str, Any],
    transform: Callable[[str], str],
    *,
    dry_run: bool,
) -> NormalisationResult:
    selectors, rendered_selector = selectors_for(config, selection.source_type)
    if selection.source_type == ".eaf":
        counts = transform_eaf(selection.path, output_path, selectors, transform, dry_run=dry_run)
    elif selection.source_type == ".textgrid":
        counts = transform_textgrid(selection.path, output_path, selectors, transform, dry_run=dry_run)
    else:
        delimiter, has_header, selectors = table_settings(config, selection.source_type)
        counts = transform_table(
            selection.path,
            output_path,
            delimiter,
            has_header,
            selectors,
            transform,
            dry_run=dry_run,
        )
    seen, changed, empty, samples = counts
    return NormalisationResult(
        source_path=selection.path,
        data_root=selection.data_root,
        output_path=output_path,
        run_dir=output_path.parents[len(selection.path.relative_to(selection.data_root).parts) - 1],
        source_type=selection.source_type,
        selected=True,
        selector=rendered_selector,
        annotations_seen=seen,
        annotations_changed=changed,
        empty_after_normalisation_count=empty,
        warnings=[],
        samples=samples,
    )


def skipped_result(selection: SourceSelection) -> NormalisationResult:
    return NormalisationResult(
        source_path=selection.path,
        data_root=selection.data_root,
        output_path=None,
        run_dir=None,
        source_type=selection.source_type,
        selected=False,
        selector="",
        annotations_seen=0,
        annotations_changed=0,
        empty_after_normalisation_count=0,
        warnings=[selection.warning],
        samples=[],
    )


def current_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H-%M-%S")


def write_summary_csv(
    path: Path, results: list[NormalisationResult], run_timestamp: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=False)
    with path.open("x", encoding="utf-8", newline="") as handle:
        fields = [
            "run_timestamp", "data_root", "source_path", "selected", "source_type",
            "selector", "output_path", "annotations_seen", "annotations_changed",
            "empty_after_normalisation_count", "warnings",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "run_timestamp": run_timestamp,
                    "data_root": result.data_root.as_posix(),
                    "source_path": result.source_path.relative_to(result.data_root).as_posix(),
                    "selected": str(result.selected).lower(),
                    "source_type": result.source_type,
                    "selector": result.selector,
                    "output_path": (
                        result.output_path.relative_to(result.run_dir).as_posix()
                        if result.output_path and result.run_dir else ""
                    ),
                    "annotations_seen": result.annotations_seen,
                    "annotations_changed": result.annotations_changed,
                    "empty_after_normalisation_count": result.empty_after_normalisation_count,
                    "warnings": "; ".join(result.warnings),
                }
            )


def run_normalisation(
    config: dict[str, Any],
    config_path: Path,
    *,
    dry_run: bool,
    timestamp: str | None = None,
) -> tuple[list[NormalisationResult], dict[Path, Path], Path]:
    validate_config(config)
    project_dir = config_path.resolve().parent.parent
    data_roots = read_data_roots(config, project_dir)
    formats = preferred_formats(config)
    patterns = require_list(config, "transcription_globs", ["**/*"])
    selections = discover_sources(data_roots, patterns, formats)
    selected = [item for item in selections if item.selected]
    if not selected:
        raise ValueError("No transcription files matched the selected prefer formats")

    timestamp = timestamp or current_timestamp()
    if not RUN_TIMESTAMP_RE.fullmatch(timestamp):
        raise ValueError(f"Invalid run timestamp: {timestamp}")
    active_roots = sorted({item.data_root for item in selected})
    run_dirs = {root: root / "normalised" / timestamp for root in active_roots}
    language = str(config.get("language", "language"))
    if language in {"", ".", ".."} or Path(language).name != language:
        raise ValueError("language must be a single non-empty path segment")
    logs_root = resolve_path(config.get("logs_dir", "output/logs"), project_dir)
    log_dir = logs_root / language / timestamp
    destinations = [*run_dirs.values(), log_dir]
    existing = [path for path in destinations if path.exists()]
    if existing:
        raise FileExistsError("Timestamped destination already exists: " + ", ".join(map(str, existing)))

    policy = build_policy(config)
    transform = lambda text: normalise_text(text, policy)

    # Parse and validate every selected source before creating any destination.
    preview_results: list[NormalisationResult] = []
    for selection in selections:
        if not selection.selected:
            preview_results.append(skipped_result(selection))
            continue
        relative = selection.path.relative_to(selection.data_root)
        output_path = run_dirs[selection.data_root] / relative
        preview_results.append(
            transform_source(selection, output_path, config, transform, dry_run=True)
        )

    if dry_run:
        return preview_results, run_dirs, log_dir

    for run_dir in run_dirs.values():
        run_dir.mkdir(parents=True, exist_ok=False)

    results: list[NormalisationResult] = []
    for selection in selections:
        if not selection.selected:
            results.append(skipped_result(selection))
            continue
        relative = selection.path.relative_to(selection.data_root)
        output_path = run_dirs[selection.data_root] / relative
        results.append(
            transform_source(selection, output_path, config, transform, dry_run=False)
        )

    summary_path = log_dir / f"{language}_normalisation_summary.csv"
    write_summary_csv(summary_path, results, timestamp)
    return results, run_dirs, log_dir


def print_results(
    results: list[NormalisationResult], run_dirs: dict[Path, Path], log_dir: Path, dry_run: bool
) -> None:
    prefix = "Dry run: no files or logs written.\n" if dry_run else ""
    if prefix:
        print(prefix, end="")
    print(f"Selected files: {sum(result.selected for result in results)}")
    print(f"Skipped files: {sum(not result.selected for result in results)}")
    print(f"Annotations/cells seen: {sum(result.annotations_seen for result in results)}")
    print(f"Annotations/cells changed: {sum(result.annotations_changed for result in results)}")
    for root, run_dir in run_dirs.items():
        print(f"Normalised data for {root}: {run_dir}")
    print(f"Logs: {log_dir}")
    if dry_run:
        for result in results:
            for original, updated in result.samples[:3]:
                print(f"  {result.source_path}: {original} -> {updated}")


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = read_simple_yaml(config_path)
    results, run_dirs, log_dir = run_normalisation(
        config, config_path, dry_run=args.dry_run
    )
    print_results(results, run_dirs, log_dir, args.dry_run)


if __name__ == "__main__":
    main()
