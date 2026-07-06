#!/usr/bin/env python3
"""Build a normalization specification from transcription source values."""

from __future__ import annotations

import argparse
import html
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("config/normalization_spec.yaml")
QUOTE_CHARS = set("\"'`´‘’‚‛“”„‟«»‹›")
IPA_MODIFIER_CHARS = {"ʰ", "ʲ", "ʷ", "ː", "ˑ", "ˀ", "ˁ", "ˤ", "ʼ"}
TONE_NUMBER_RE = re.compile(r"(?<!\d)([1-5]{2})(?!\d)")


@dataclass(frozen=True)
class TranscriptionSource:
    path: Path
    source_type: str
    values: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Spec YAML config. Default: {DEFAULT_CONFIG}",
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


def require_list(config: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = config.get(key, default)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a YAML list or string")
    return [str(item) for item in value]


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


def reverse_replacement_rules(rules: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
    reverse: list[tuple[str, str]] = []
    target_to_source: dict[str, str] = {}
    warnings: list[str] = []
    for source, target in rules:
        if not target:
            warnings.append(f"Cannot reverse deletion rule {source!r}=>{target!r}.")
            continue
        existing = target_to_source.get(target)
        if existing is not None and existing != source:
            warnings.append(
                f"Cannot safely reverse {target!r}: both {existing!r} and {source!r} map to it."
            )
            continue
        target_to_source[target] = source
    for target, source in target_to_source.items():
        reverse.append((target, source))
    return reverse, warnings


def replace_by_rules(text: str, rules: list[tuple[str, str]]) -> str:
    for source, target in sorted(rules, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, target)
    return text


def rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def read_eaf_values(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    values: list[str] = []
    for element in root.iter():
        if strip_namespace(element.tag) != "ANNOTATION_VALUE":
            continue
        text = "".join(element.itertext()).strip()
        if text:
            values.append(html.unescape(text))
    return values


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
        globbed = {path for path in data_root.glob(pattern) if path.is_file()}
        paths.update(globbed)
        if "**" not in pattern:
            paths.update(path for path in data_root.rglob(pattern) if path.is_file())
    return sorted(paths)


def recording_key(path: Path) -> str:
    stem = path.stem
    if path.suffix.lower() == ".txt":
        return re.sub(r"-[A-Z]$", "", stem)
    return stem


def choose_preferred_paths(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    by_recording: dict[str, list[Path]] = {}
    for path in paths:
        by_recording.setdefault(recording_key(path), []).append(path)

    chosen: list[Path] = []
    skipped: list[Path] = []
    for group_paths in by_recording.values():
        eaf_paths = sorted(path for path in group_paths if path.suffix.lower() == ".eaf")
        if eaf_paths:
            chosen.extend(eaf_paths)
            skipped.extend(path for path in sorted(group_paths) if path.suffix.lower() != ".eaf")
        else:
            chosen.extend(sorted(group_paths))
    return sorted(chosen), sorted(skipped)


def read_transcription_sources(
    data_root: Path,
    patterns: list[str],
    *,
    prefer_eaf: bool,
) -> tuple[list[TranscriptionSource], list[Path]]:
    paths = [
        path
        for path in find_transcription_paths(data_root, patterns)
        if path.suffix.lower() in {".eaf", ".txt"}
    ]
    skipped: list[Path] = []
    if prefer_eaf:
        paths, skipped = choose_preferred_paths(paths)

    sources: list[TranscriptionSource] = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix == ".eaf":
            source_type = "eaf"
            values = read_eaf_values(path)
        elif suffix == ".txt":
            source_type = "txt"
            values = read_txt_values(path)
        else:
            continue
        sources.append(TranscriptionSource(path=path, source_type=source_type, values=values))
    return sources, skipped


def char_display(char: str) -> str:
    if char == " ":
        return "SPACE"
    if char == "\t":
        return "TAB"
    return char


def markdown_char(char: str) -> str:
    display = char_display(char)
    if display == "`":
        return "`` ` ``"
    return f"`{display}`"


def char_name(char: str) -> str:
    if char == " ":
        return "SPACE"
    if char == "\t":
        return "TAB"
    return unicodedata.name(char, "UNKNOWN")


def char_category(char: str) -> str:
    if char.isspace():
        return "whitespace"
    if char.isdigit():
        return "tone/digit"
    if char.isupper():
        return "uppercase"
    if char in QUOTE_CHARS:
        return "quote"
    if char in IPA_MODIFIER_CHARS:
        return "ipa_modifier"
    if unicodedata.category(char) == "Mn":
        return "combining_diacritic"
    if unicodedata.category(char).startswith("P"):
        return "punctuation"
    if unicodedata.category(char).startswith("S"):
        return "symbol"
    if char.isalpha():
        return "letter"
    return "other"


def normalize_preview(text: str, policy: dict[str, Any]) -> str:
    for step in policy["normalization_steps"]:
        if step == "remove_bracketed_text":
            text = re.sub(r"\[[^\]]*\]", " ", text)
            text = re.sub(r"\([^)]*\)", " ", text)
        elif step == "replace_sequences":
            text = replace_by_rules(text, policy["sequence_replacements"])
        elif step == "map_tone_numbers":
            text = replace_by_rules(text, policy["tone_replacements"])
        elif step == "remove_configured_punctuation":
            remove_chars = set(policy["remove_chars"])
            text = "".join(" " if char in remove_chars else char for char in text)
        elif step == "remove_hash_markers":
            if policy["remove_hash"]:
                text = text.replace("#", " ")
        elif step == "remove_punctuation":
            if policy["remove_punctuation"]:
                text = "".join(
                    " " if unicodedata.category(char).startswith("P") else char
                    for char in text
                )
        elif step == "collapse_whitespace":
            if policy["collapse_whitespace"]:
                text = re.sub(r"\s+", " ", text).strip()
        elif step == "strip_spaces":
            if policy["strip_spaces"]:
                text = re.sub(r"\s+", "", text)
        else:
            raise ValueError(f"Unknown normalization step: {step}")
    return text


def analyze(sources: list[TranscriptionSource]) -> dict[str, Any]:
    char_counts: Counter[str] = Counter()
    tone_counts: Counter[str] = Counter()
    uppercase_counts: Counter[str] = Counter()
    punctuation_counts: Counter[str] = Counter()
    quote_counts: Counter[str] = Counter()
    ipa_modifier_counts: Counter[str] = Counter()
    combining_counts: Counter[str] = Counter()
    file_rows: list[dict[str, Any]] = []
    source_type_counts: Counter[str] = Counter()

    total_values = 0
    total_chars = 0
    examples: list[str] = []

    for source in sources:
        source_type_counts[source.source_type] += 1
        file_char_counts: Counter[str] = Counter()
        file_tone_counts: Counter[str] = Counter()

        for value in source.values:
            total_values += 1
            total_chars += len(value)
            if len(examples) < 8:
                examples.append(value)
            char_counts.update(value)
            file_char_counts.update(value)

            for char in value:
                if char.isupper():
                    uppercase_counts[char] += 1
                if unicodedata.category(char).startswith("P"):
                    punctuation_counts[char] += 1
                if char in QUOTE_CHARS:
                    quote_counts[char] += 1
                if char in IPA_MODIFIER_CHARS:
                    ipa_modifier_counts[char] += 1
                if unicodedata.category(char) == "Mn":
                    combining_counts[char] += 1

            for tone in TONE_NUMBER_RE.findall(value):
                tone_counts[tone] += 1
                file_tone_counts[tone] += 1

        file_rows.append(
            {
                "path": source.path,
                "source_type": source.source_type,
                "entries": len(source.values),
                "unique_chars": len(file_char_counts),
                "tone_counts": file_tone_counts,
            }
        )

    return {
        "char_counts": char_counts,
        "tone_counts": tone_counts,
        "uppercase_counts": uppercase_counts,
        "punctuation_counts": punctuation_counts,
        "quote_counts": quote_counts,
        "ipa_modifier_counts": ipa_modifier_counts,
        "combining_counts": combining_counts,
        "file_rows": file_rows,
        "source_type_counts": source_type_counts,
        "total_values": total_values,
        "total_chars": total_chars,
        "examples": examples,
    }


def decision_for_char(char: str, policy: dict[str, Any]) -> tuple[str, str]:
    category = char_category(char)

    if char.isspace():
        return "map", "Collapse all whitespace to single spaces."
    if char == "#":
        return ("remove" if policy["remove_hash"] else "review", "Annotation marker, not speech text.")
    if char in {"?", "？", ",", ".", "(", ")"}:
        return ("remove" if policy["remove_punctuation"] else "review", "Annotation punctuation, not needed for ASR target text.")
    if char.isdigit():
        return ("keep", "Tone digits are part of the current transcription system.")
    if char == "N" and policy["preserve_uppercase_n"]:
        return "keep", "Uppercase N appears to encode a meaningful transcription symbol."
    if char.isupper():
        return "review", "Unexpected uppercase letter."
    if char in IPA_MODIFIER_CHARS:
        return ("keep" if not policy["remove_ipa_modifiers"] else "remove", "IPA modifier character.")
    if unicodedata.category(char) == "Mn":
        return ("keep" if not policy["remove_diacritics"] else "remove", "Combining diacritic.")
    if category in {"letter", "ipa_modifier"}:
        return "keep", "Speech transcription character."
    if category in {"punctuation", "symbol", "quote"}:
        return "review", "Non-letter symbol not covered by default policy."
    return "review", "Unclassified character."


def counter_table(counter: Counter[str], empty_message: str = "No matches found.") -> list[str]:
    if not counter:
        return [empty_message]
    lines = ["| Character | Count | Unicode name |", "| --- | ---: | --- |"]
    for char, count in counter.most_common():
        lines.append(f"| {markdown_char(char)} | {count:,} | {char_name(char)} |")
    return lines


def replacement_table(rules: list[tuple[str, str]]) -> list[str]:
    if not rules:
        return ["No replacement rules configured."]
    lines = ["| Source | Target |", "| --- | --- |"]
    for source, target in rules:
        lines.append(f"| `{source}` | `{target}` |")
    return lines


def tone_table(counter: Counter[str]) -> list[str]:
    if not counter:
        return ["No tone number sequences found."]
    lines = ["| Tone number | Count | Decision |", "| --- | ---: | --- |"]
    for tone, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        decision = "keep"
        if tone not in {"33", "35", "53", "55"}:
            decision = "review"
        lines.append(f"| `{tone}` | {count:,} | {decision} |")
    return lines


def character_decision_table(char_counts: Counter[str], policy: dict[str, Any]) -> list[str]:
    lines = ["| Character | Count | Category | Decision | Reason |", "| --- | ---: | --- | --- | --- |"]
    for char, count in char_counts.most_common():
        decision, reason = decision_for_char(char, policy)
        lines.append(
            f"| {markdown_char(char)} | {count:,} | {char_category(char)} | {decision} | {reason} |"
        )
    return lines


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_normalization_yaml(
    config: dict[str, Any],
    analysis: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    char_counts: Counter[str] = analysis["char_counts"]
    keep_chars: list[str] = []
    remove_chars: list[str] = []
    review_chars: list[str] = []
    forward_rules = policy["sequence_replacements"] + policy["tone_replacements"]
    reverse_rules, reverse_warnings = reverse_replacement_rules(forward_rules)
    reverse_steps = ["reverse_replacements"]

    for char in sorted(char_counts):
        decision, _ = decision_for_char(char, policy)
        if decision == "keep":
            keep_chars.append(char)
        elif decision == "remove":
            remove_chars.append(char)
        elif decision == "review":
            review_chars.append(char)

    lines = [
        "# Generated by scripts/build_normalization_spec.py.",
        f"language: {config['language']}",
        "source_formats:",
        "  - eaf",
        "  - txt",
        "source_preference: eaf",
        f"generated_on: {date.today().isoformat()}",
        "policy:",
        f"  preserve_tone_numbers: {str(policy['preserve_tone_numbers']).lower()}",
        f"  preserve_ipa_letters: {str(policy['preserve_ipa_letters']).lower()}",
        f"  preserve_uppercase_n: {str(policy['preserve_uppercase_n']).lower()}",
        f"  remove_punctuation: {str(policy['remove_punctuation']).lower()}",
        f"  remove_hash: {str(policy['remove_hash']).lower()}",
        f"  collapse_whitespace: {str(policy['collapse_whitespace']).lower()}",
        f"  strip_spaces: {str(policy['strip_spaces']).lower()}",
        f"  remove_diacritics: {str(policy['remove_diacritics']).lower()}",
        f"  remove_ipa_modifiers: {str(policy['remove_ipa_modifiers']).lower()}",
        "forward_pipeline:",
    ]
    for step in policy["normalization_steps"]:
        lines.append(f"  - {step}")
    lines.extend(
        [
            "reverse_pipeline:",
        ]
    )
    for step in reverse_steps:
        lines.append(f"  - {step}")
    lines.extend(
        [
            "sequence_replacements:",
        ]
    )
    for source, target in policy["sequence_replacements"]:
        lines.append(f"  - source: {yaml_quote(source)}")
        lines.append(f"    target: {yaml_quote(target)}")
    lines.append("tone_replacements:")
    for source, target in policy["tone_replacements"]:
        lines.append(f"  - source: {yaml_quote(source)}")
        lines.append(f"    target: {yaml_quote(target)}")
    lines.append("reverse_replacements:")
    for source, target in reverse_rules:
        lines.append(f"  - source: {yaml_quote(source)}")
        lines.append(f"    target: {yaml_quote(target)}")
    if reverse_warnings:
        lines.append("reverse_warnings:")
        for warning in reverse_warnings:
            lines.append(f"  - {yaml_quote(warning)}")
    else:
        lines.append("reverse_warnings: []")
    lines.extend(
        [
            "remove_chars:",
        ]
    )
    for char in policy["remove_chars"]:
        lines.append(f"  - {yaml_quote(char)}")
    lines.extend(
        [
        "tone_numbers:",
        ]
    )
    for tone, count in sorted(analysis["tone_counts"].items(), key=lambda item: (-item[1], item[0])):
        decision = "keep" if tone in {"33", "35", "53", "55"} else "review"
        lines.append(f"  - value: {yaml_quote(tone)}")
        lines.append(f"    count: {count}")
        lines.append(f"    decision: {decision}")

    lines.extend(["characters:", "  keep:"])
    for char in keep_chars:
        lines.append(f"    - {yaml_quote(char)}")
    lines.append("  remove:")
    for char in remove_chars:
        lines.append(f"    - {yaml_quote(char)}")
    if review_chars:
        lines.append("  review:")
        for char in review_chars:
            lines.append(f"    - {yaml_quote(char)}")
    else:
        lines.append("  review: []")
    lines.append("notes:")
    lines.append('  - "Apply forward_pipeline for training text normalization."')
    lines.append('  - "Apply reverse_pipeline after model output to restore configured symbols where possible."')
    return "\n".join(lines) + "\n"


def build_report(
    config: dict[str, Any],
    config_path: Path,
    data_root: Path,
    transcription_globs: list[str],
    report_path: Path,
    normalization_config_path: Path,
    sources: list[TranscriptionSource],
    skipped_preferred_paths: list[Path],
    analysis: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    language = str(config.get("language", data_root.name))
    char_counts: Counter[str] = analysis["char_counts"]
    source_type_counts: Counter[str] = analysis["source_type_counts"]
    lines = [
        f"# {config['title']}",
        "",
        f"Date: {date.today().isoformat()}",
        "",
        "This report defines a normalization policy from raw transcription values.",
        "When both `.eaf` and `.txt` files exist for the same recording, `.eaf` is preferred.",
        "",
        "## Inputs And Outputs",
        "",
        f"- Config: `{rel(config_path, config_path.parent.parent)}`",
        f"- Data root: `{data_root}`",
        f"- Transcription globs: `{', '.join(transcription_globs)}`",
        f"- Files scanned: {len(sources):,}",
        f"- EAF files scanned: {source_type_counts.get('eaf', 0):,}",
        f"- TXT files scanned: {source_type_counts.get('txt', 0):,}",
        f"- TXT files skipped because EAF was preferred: {len(skipped_preferred_paths):,}",
        f"- Transcription values scanned: {analysis['total_values']:,}",
        f"- Characters counted: {analysis['total_chars']:,}",
        f"- Unique characters: {len(char_counts):,}",
        f"- Report output: `{report_path}`",
        f"- Generated normalization config: `{normalization_config_path}`",
        "",
        "## Source Files",
        "",
        "| File | Type | Values | Unique chars | Tone numbers |",
        "| --- | --- | ---: | ---: | --- |",
    ]

    for row in analysis["file_rows"]:
        tones = ", ".join(
            f"{tone}:{count}" for tone, count in sorted(row["tone_counts"].items())
        ) or "-"
        lines.append(
            f"| `{rel(row['path'], data_root)}` | {row['source_type']} | {row['entries']:,} | {row['unique_chars']:,} | {tones} |"
        )

    if skipped_preferred_paths:
        lines.extend(["", "TXT files skipped because EAF was preferred:", ""])
        for path in skipped_preferred_paths:
            lines.append(f"- `{rel(path, data_root)}`")

    lines.extend(["", "## Recommended Default Policy", ""])
    lines.extend(
        [
            "- Use `.eaf` annotation values as the authoritative transcription source when available.",
            "- Use `.txt` transcription exports only when no matching `.eaf` file is present.",
            "- Apply normalization as an ordered, editable pipeline.",
            "- Convert configured multi-character transcription symbols into single-character training symbols.",
            "- Convert configured tone numbers into single-character training symbols.",
            "- Strip spaces as an explicit training-text step.",
            "- Generate reverse mappings for every one-to-one replacement rule.",
            "- Remove annotation punctuation from ASR training text: commas, question marks, fullwidth question marks, full stops, and parentheses.",
            "- Do not remove diacritics.",
        ]
    )

    lines.extend(["", "## Forward Normalization Pipeline", ""])
    for idx, step in enumerate(policy["normalization_steps"], start=1):
        lines.append(f"{idx}. `{step}`")

    lines.extend(["", "## Sequence Replacement Rules", ""])
    lines.extend(replacement_table(policy["sequence_replacements"]))

    lines.extend(["", "## Tone Replacement Rules", ""])
    lines.extend(replacement_table(policy["tone_replacements"]))

    reverse_rules, reverse_warnings = reverse_replacement_rules(
        policy["sequence_replacements"] + policy["tone_replacements"]
    )
    lines.extend(["", "## Reverse Replacement Rules", ""])
    lines.extend(replacement_table(reverse_rules))
    if reverse_warnings:
        lines.extend(["", "Reverse warnings:", ""])
        for warning in reverse_warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "## Tone Numbers", ""])
    lines.extend(tone_table(analysis["tone_counts"]))

    lines.extend(["", "## Uppercase Letters", ""])
    lines.extend(counter_table(analysis["uppercase_counts"]))

    lines.extend(["", "## Punctuation And Symbols", ""])
    lines.extend(counter_table(analysis["punctuation_counts"]))

    lines.extend(["", "## Quotation Marks And Apostrophes", ""])
    lines.extend(counter_table(analysis["quote_counts"]))

    lines.extend(["", "## IPA Modifier Characters", ""])
    lines.extend(counter_table(analysis["ipa_modifier_counts"]))

    lines.extend(["", "## Combining Diacritics", ""])
    lines.extend(counter_table(analysis["combining_counts"]))

    lines.extend(["", "## Character Decisions", ""])
    lines.extend(character_decision_table(char_counts, policy))

    lines.extend(["", "## Example Normalization", ""])
    if analysis["examples"]:
        lines.extend(["| Original | Normalized preview |", "| --- | --- |"])
        for example in analysis["examples"]:
            preview = normalize_preview(example, policy)
            lines.append(f"| `{example}` | `{preview}` |")
    else:
        lines.append("No annotation values found.")

    lines.extend(
        [
            "",
            "## Checkpoint",
            "",
            "- Output is a normalization spec only.",
            f"- {language} was treated as read-only input.",
            "- Next exact step: use `config/yonghe_qiang_normalization.yaml` to build a separate prepared-manifest phase.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    config = read_simple_yaml(config_path)
    config_dir = config_path.parent.parent
    language = config.get("language")

    data_root = resolve_path(config["data_root"], config_dir)
    normalization_config_path = resolve_path(config["normalization_config_path"], config_dir)
    transcription_globs = config.get("transcription_globs", config.get("eaf_glob", ["**/*.eaf"]))
    if isinstance(transcription_globs, str):
        transcription_globs = [transcription_globs]
    if not isinstance(transcription_globs, list):
        raise ValueError("transcription_globs must be a YAML list or string")
    prefer_eaf = bool(config.get("prefer_eaf", True))

    if "report_path" in config:
        report_path = resolve_path(config["report_path"], config_dir)
    else:
        report_dir = resolve_path(config.get("report_dir", "reports"), config_path.parent)
        report_name = str(config.get("report_name", "{language}_normalization_spec.md")).format(
            language=config["language"]
        )
        report_path = report_dir / report_name

    policy = {
        "preserve_tone_numbers": bool(config.get("preserve_tone_numbers", True)),
        "preserve_ipa_letters": bool(config.get("preserve_ipa_letters", True)),
        "preserve_uppercase_n": bool(config.get("preserve_uppercase_n", True)),
        "remove_punctuation": bool(config.get("remove_punctuation", True)),
        "remove_hash": bool(config.get("remove_hash", True)),
        "collapse_whitespace": bool(config.get("collapse_whitespace", True)),
        "strip_spaces": bool(config.get("strip_spaces", False)),
        "remove_diacritics": bool(config.get("remove_diacritics", False)),
        "remove_ipa_modifiers": bool(config.get("remove_ipa_modifiers", False)),
        "normalization_steps": require_list(
            config,
            "normalization_steps",
            [
                "remove_bracketed_text",
                "remove_configured_punctuation",
                "remove_hash_markers",
                "collapse_whitespace",
            ],
        ),
        "sequence_replacements": parse_replacement_rules(
            require_list(config, "sequence_replacements", [])
        ),
        "tone_replacements": parse_replacement_rules(
            require_list(config, "tone_replacements", [])
        ),
        "remove_chars": require_list(
            config,
            "remove_chars",
            [",", "?", "？", ".", "(", ")", "#"],
        ),
    }

    sources, skipped_preferred_paths = read_transcription_sources(
        data_root,
        [str(pattern) for pattern in transcription_globs],
        prefer_eaf=prefer_eaf,
    )
    analysis = analyze(sources)

    report = build_report(
        config=config,
        config_path=config_path,
        data_root=data_root,
        transcription_globs=[str(pattern) for pattern in transcription_globs],
        report_path=report_path,
        normalization_config_path=normalization_config_path,
        sources=sources,
        skipped_preferred_paths=skipped_preferred_paths,
        analysis=analysis,
        policy=policy,
    )
    normalization_yaml = build_normalization_yaml(config, analysis, policy)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    normalization_config_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    normalization_config_path.write_text(normalization_yaml, encoding="utf-8")

    print(f"Wrote {report_path}")
    print(f"Wrote {normalization_config_path}")


if __name__ == "__main__":
    main()
