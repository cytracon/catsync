#!/usr/bin/env python3
"""Minimal pytest-compatible runner (no pytest package required)."""
from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

pytest = ModuleType("pytest")


class _Raises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, typ, val, tb):
        if typ is None:
            raise AssertionError(f"DID NOT RAISE {self.exc}")
        return issubclass(typ, self.exc)


pytest.raises = _Raises  # type: ignore[attr-defined]
sys.modules["pytest"] = pytest

passed = 0
failed = 0
errors: list[str] = []


def run_file(path: Path) -> None:
    global passed, failed
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_"):
            continue
        params = inspect.signature(fn).parameters
        kwargs = {}
        tmp = None
        if "tmp_path" in params:
            tmp = tempfile.TemporaryDirectory()
            kwargs["tmp_path"] = Path(tmp.name)
        try:
            fn(**kwargs)
            passed += 1
            print(f"  PASS {path.name}::{name}")
        except Exception:
            failed += 1
            errors.append(f"{path.name}::{name}\n{traceback.format_exc()}")
            print(f"  FAIL {path.name}::{name}")
        finally:
            if tmp is not None:
                tmp.cleanup()


def main() -> int:
    tests = sorted((ROOT / "tests").glob("test_*.py"))
    print(f"Running {len(tests)} files")
    for path in tests:
        run_file(path)
    print(f"\n{passed} passed, {failed} failed")
    for err in errors:
        print("---")
        print(err)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
