# Normalization Spec Model

This folder builds a normalization specification from raw EAF transcription data.

It is intentionally separate from `../yonghe-qiang/`: the script reads EAF files
from that folder, but writes all reports and generated config here.

## Run

From `asr-models/normalization-spec-model/`:

```bash
python scripts/build_normalization_spec.py --config config/normalization_spec.yaml
```

Outputs:

```text
reports/yonghe_qiang_normalization_spec.md
config/yonghe_qiang_normalization.yaml
```

The report is for human review. The generated YAML is the machine-readable
normalization policy to use in the next phase.

