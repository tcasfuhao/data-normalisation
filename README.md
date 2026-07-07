# Data Normalisation

This folder normalises language transcription data.

The scripts read source EAF/TXT files from that folder, but write generated files here.

## Run

```bash
python scripts/normalise_data.py --config config/normalization_spec.yaml
```

Outputs:

```text
output/eaf/
output/logs/normalisation_summary.csv
```

The runner prefers `.eaf` over matching `.txt` files. It copies EAF documents to `output/eaf/` and changes only `ANNOTATION_VALUE` text. Matching `.txt` files are skipped without being normalised. Tone replacement rules remain configurable, but they are only applied when `map_tones` is `false`. The ordered `normalization_steps` use the same names as the policy flags where a step is policy-controlled, including `remove_bracketed_text`, `replace_sequences`, and `lowercase_text`.

Use `--dry-run` to print counts and sample transformations without writing normalised EAF files:

```bash
python scripts/normalise_data.py --config config/normalization_spec.yaml --dry-run
```

To reverse the configured one-to-one symbol replacements in normalised EAF files:

```bash
python scripts/undoing_changes.py --config config/normalization_spec.yaml
```

Undo output is written to `output/undo/eaf/`. This only reverses replacement arrows such as `ƙ=>kʰ`; removed punctuation, spaces, hashes, and bracketed text cannot be reconstructed.

Use `--dry-run` to print counts and sample transformations without writing normalised EAF files:

```bash
python scripts/undoing_changes.py --config config/normalization_spec.yaml --dry-run
```
