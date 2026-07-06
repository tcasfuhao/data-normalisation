# Data Normalisation

This folder normalises configured Yonghe Qiang transcription data.

It is intentionally separate from `../yonghe-qiang/`: the scripts read source
EAF/TXT files from that folder, but write generated files here.

## Run

From `asr-models/data-normalisation/`:

```bash
python scripts/normalise_data.py --config config/normalization_spec.yaml
```

Outputs:

```text
output/eaf/
output/logs/normalisation_summary.csv
```

The runner prefers `.eaf` over matching `.txt` files. It copies EAF documents
to `output/eaf/` and changes only `ANNOTATION_VALUE` text. Source files under
`../yonghe-qiang/` are not edited. Matching `.txt` files are skipped without
being normalised.

Use `--dry-run` to print counts and sample transformations without writing
normalised EAF files:

```bash
python scripts/normalise_data.py --config config/normalization_spec.yaml --dry-run
```

To reverse the configured one-to-one symbol replacements in normalised EAF files:

```bash
python scripts/undoing_changes.py --config config/normalization_spec.yaml
```

Undo output is written to `output/undo/eaf/`. This only reverses replacement
arrows such as `ƙ=>kʰ`; removed punctuation, spaces, hashes, and bracketed text
cannot be reconstructed.
