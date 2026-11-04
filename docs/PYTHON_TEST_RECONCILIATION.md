# Python Test Reconciliation

Reproduced with `python scripts/reconcile_tests.py` on the delivered ZIP
`bybit-l2-feasibility-evidence.zip`, fresh venvs, OS `win32`, command `python -m pytest -q -rs`.

| Interpreter | `pip install -e .[dev]` | Collected, no data | Result, no data | Collected, data | Result, data |
|---|---|---|---|---|---|
| Python 3.12.10 | accepted | 147 | 147 passed, 1 skipped | 151 | 151 passed |
| Python 3.13.18 | refused | 147 | 147 passed, 1 skipped | 151 | 151 passed |

## Cause (proven by collection, not assumed)

The two reported figures are the same test suite in two states:

* **147 passed, 1 skipped** is a run **without raw data**, which is the normal state of the
  ZIP and of a fresh clone. `tests/integration/test_real_sample.py` called
  `pytest.skip(..., allow_module_level=True)` at import, so its 4 tests were never
  collected and the whole module counted as 1 skip.
* **151 passed** is the same run **with raw data** downloaded: 147 + the 4 real-data tests.

Skip line recorded: `SKIPPED [1] tests\integration\test_real_sample.py:28: real sample not downloaded (run verify-data)`

* Python version: not the cause. 3.12 and 3.13 give the same counts in both states.
* ZIP vs GitHub: the ZIP was built from commit `4219c02` using `git ls-files`, so its tests
  are that commit's tests.
* Scripts: none are counted by pytest.
* Optional dependencies: none affect collection.

We have not seen the client's environment, so the match is by exact counts. The client
report of 147/1 is exactly the no-data state.

## Separate finding: Python 3.13

The delivered `pyproject.toml` had `requires-python = ">=3.12,<3.13"`, so a plain install on
3.13 was refused, and the 3.13 run above used `--ignore-requires-python`. A full replay on
3.13.18 then produced byte-identical analytical files
(4/4,
`evidence/python313_replay_check.json`), so the limit is now `<3.14`. Only Windows was tested.

## What changed

The real-data tests now skip per test, so they are always collected: a run without data
reports each of them as skipped instead of hiding them.

Current suite: `192 passed in 210.31s (0:03:30)`, branch coverage 92%.

Lock file: `requirements-lock.txt`. Dependencies in the reconciliation run:
`annotated-types==0.8.0`, `anyio==4.16.0`, `ast_serialize==0.13.1`, `certifi==2026.10.23`, `colorama==0.4.6`, `coverage==7.17.1`, `h11==0.16.0`, `httpcore==1.0.9`, `httpx==0.28.1`, `idna==3.20`, `iniconfig==2.3.1`, `librt==0.16.0`, `markdown-it-py==4.2.0`, `mdurl==0.1.2`, `mypy==2.4.1`, `mypy_extensions==1.2.0`, `packaging==26.3`, `pathspec==1.1.1`, `pluggy==1.7.0`, `pydantic==2.14.2`, `pydantic_core==2.50.2`, `Pygments==2.21.0`, `pytest==9.2.0`, `pytest-asyncio==1.4.1.post1`, `pytest-cov==7.1.0`, `PyYAML==6.0.3`, `rich==15.0.0`, `ruff==0.17.3`, `types-PyYAML==6.0.12.20261103`, `typing-inspection==0.4.4`, `typing_extensions==4.16.0`.

```bash
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements-lock.txt
pip install -e ".[dev]"
python -m pytest -q -rs
```
