# SGLang 0.5.18: `schedule_policy.py` (unmodified)

One source file from SGLang 0.5.18, vendored without any change, plus the license it ships under.

| File | Origin | sha256 |
|---|---|---|
| `schedule_policy.py` | `sglang/srt/managers/schedule_policy.py` from the PyPI wheel `sglang-0.5.18-cp312-cp312-manylinux_2_34_x86_64.whl`. Identical to `python/sglang/srt/managers/schedule_policy.py` at tag `v0.5.18` of [sgl-project/sglang](https://github.com/sgl-project/sglang) (commit `71de97b264b04dcd514cf904003028aefe9775c8`). 62,823 bytes. | `7fa154986e574cabdbc341875401a59a7a388f94c548f11bf3d2bd7badc131d4` |
| `LICENSE` | `sglang-0.5.18.dist-info/licenses/LICENSE` from the same wheel: Apache License 2.0, Copyright 2023-2024 SGLang Team. | `1495e1e757ef4d0925a2350563cf5754bb23c51701a8ec4fb3c5cdcbedae6747` |

The wheel itself has sha256 `fba7bb31a5de4c014945cbc50e9357c9253cc965a423736c324b2aaccd2270dc`, the value PyPI publishes for that file. `SHA256SUMS` in this directory lists both files.

## Why it is here

The CPU tests in [`tests/`](../../tests/) need the exact upstream source of `PrefillAdder`:

- they apply the `schedule_policy.py` part of [`patch/peak_kv_reservation.diff`](../../patch/peak_kv_reservation.diff) to a copy of this file in a temporary directory;
- they compare the patched admission gate with upstream's own `add_one_req_ignore_eos`.

Importing SGLang needs torch, `sgl_kernel` and a GPU stack. So the tests never import this file. They read it as text and compile only the methods under test.

The file keeps its original header and license notice. It is not modified, imported or executed as a module. It remains under SGLang's Apache License 2.0 (`LICENSE` here). Only one of the three files the diff touches is vendored. The other two (`server_args.py`, `scheduler.py`) are checked in CI against the wheel downloaded from PyPI.

## How to check it

```bash
cd third_party/sglang_v0_5_18 && sha256sum -c SHA256SUMS
python3 tests/run_cpu_tests.py      # also checks these hashes before using the file
```

On every push, CI (`.github/workflows/cpu-tests.yml`) downloads `sglang==0.5.18` from PyPI and checks the wheel's sha256. It then compares this file and `LICENSE` with the wheel's copies byte for byte.
