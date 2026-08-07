# Data Normalisation

This folder normalises language transcription data without modifying its sources.

## (Un)Required Conda environment

This project does not require a dedicated environment, however, for consistency sake, we recommend using it regardless. The conda environemnt is `tcas_asr_python3.10`. Create it once with Python 3.10, then activate it before running any command:

```bash
conda create --name tcas_asr_python3.10 python=3.10
conda activate tcas_asr_python3.10
python -m pip install -r requirements.txt
```

## Configuration

Edit `config/normalization_spec.yaml` to set the source roots, file globs, normalisation policy, and selected formats. `prefer` is a set rather than a priority order: selecting both `.eaf` and `.TextGrid` processes both formats.

```yaml
data_roots:
  - ~/projects/my-projects/asr-models/language-downloads/example
transcription_globs:
  - "*.eaf"
  - "*.TextGrid"
  - "*.csv"
  - "*.tsv"
  - "*.txt"
prefer: [.eaf, .TextGrid]

eaf_tier: transcription
textgrid_tier: [segments, words]

csv_has_header: true
csv_text_column: FORM
tsv_has_header: true
tsv_text_column: [FORM, PHONETIC]
txt_delimiter: tab
txt_has_header: false
txt_text_column: 4
```

Tier and column selectors accept one value, a YAML list, or `all`. Column names are matched case-insensitively. Numeric columns are 1-based. Headerless tables require numeric selectors or `all`; TXT files must declare `txt_delimiter` as `tab`, `comma`, or one literal delimiter character.

## Run

```bash
python scripts/normalise_data.py --config config/normalization_spec.yaml
```

Each run uses one local-time timestamp and writes data without overwriting earlier
runs:

```text
<data_root>/normalised/YYYY-MM-DD-HH-MM-SS/<source-relative-path>
logs/<language>/YYYY-MM-DD-HH-MM-SS/<language>_normalisation_summary.csv
```

Generated `normalised` and `undone` directories are excluded from later source scans. Use `--dry-run` to inspect selections, destinations, counts, and sample changes without creating data or logs.

## Undo reversible replacements

Pass each exact normalisation run explicitly; `--run-dir` can be repeated:

```bash
python scripts/undoing_changes.py \
  --config config/normalization_spec.yaml \
  --run-dir /path/to/data/normalised/2026-07-31-14-20-05
```

Undo supports EAF, TextGrid, CSV, TSV, and delimited TXT outputs. It writes to:

```text
<data_root>/undone/<normalise-timestamp>-<undo-timestamp>/
```

Only configured one-to-one replacements can be reversed. Removed characters, whitespace changes, bracket removal, and case conversion cannot be reconstructed.
