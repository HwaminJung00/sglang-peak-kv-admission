# Third-party material

This repository builds on the components below. Only what the "In this
repository" column lists is redistributed here; everything else is linked.
Licenses for this repository's own files are in [LICENSE](LICENSE) (code,
patch, tests) and [LICENSE-docs](LICENSE-docs) (docs, figures, derived data).

| Component | Version | License | In this repository | How it is used |
|---|---|---|---|---|
| [SGLang](https://github.com/sgl-project/sglang) | v0.5.18, commit `71de97b` (PyPI wheel `sglang-0.5.18-cp312-cp312-manylinux_2_34_x86_64.whl`, sha256 `fba7bb31a5de4c014945cbc50e9357c9253cc965a423736c324b2aaccd2270dc`) | Apache-2.0, Copyright 2023-2024 SGLang Team | `patch/peak_kv_reservation.diff`, a diff against three SGLang files (+102/−0, see [NOTICE](NOTICE)); `third_party/sglang_v0_5_18/schedule_policy.py`, one unmodified file with SGLang's `LICENSE` | Serving engine for every measurement. Each run used either unmodified v0.5.18 or v0.5.18 with this patch applied (the flag is off unless the run enables it). The CPU tests run the patched admission code against the vendored upstream file. The rest of SGLang is not included; install `sglang==0.5.18`. |
| [Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B) | revision `1cfa9a7208912126459214e8b04321603b3df60c` | Apache-2.0, Copyright 2024 Alibaba Cloud | Weights: not included. Generated text: in the raw-data release `data-v1` (`w3-raw-v1.tar.xz`), not in git | Served model (bf16) for every measurement; its tokenizer was used to generate the traces. |
| [mlleo/inference-engine-study](https://github.com/mlleo/inference-engine-study/tree/802a1642fcefc06735a89af054213c9904b4ff45) | commit `802a164` | None (the repository has no license file) | **Not included.** Linked for reproduction only | The course repository. It provided the workload definition, the trace generator, the replay and metrics harness, and the hint to explore peak-KV reservation. `tools/fetch_course_harness.sh` fetches `project/{bench,common,workloads,scripts}` at this commit into `.course/` (gitignored), only for GPU reproduction and trace regeneration. The offline analysis, figures and CPU tests do not need it. |

## Python dependencies (installed separately, not redistributed)

| Package | Version | License | Needed for |
|---|---|---|---|
| [NumPy](https://numpy.org/) | 2.1.2 | BSD-3-Clause | analysis scripts (`w3/`, `analysis/`) |
| [Matplotlib](https://matplotlib.org/) | 3.11.2 | Matplotlib License (PSF-based) | figures only (`python3 -m w3.figures`) |

The tests and the scripts in `tools/` use the Python standard library only.
See [requirements.txt](requirements.txt).
