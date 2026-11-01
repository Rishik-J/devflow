"""Validate agent output against the JSON schemas.

Schemas are read from the repo's `.devflow/schemas/` when present, so a repo can tighten them,
and fall back to the copies shipped with devflow.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

SHIPPED = Path(__file__).parent / "schemas"


class SchemaError(ValueError):
    pass


def load_schema(name: str, root: Path | None = None) -> dict:
    candidates = []
    if root is not None:
        candidates.append(root / ".devflow" / "schemas" / f"{name}.schema.json")
    candidates.append(SHIPPED / f"{name}.schema.json")
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text())
    raise SchemaError(f"unknown schema {name!r}")


def errors(name: str, data: object, root: Path | None = None) -> list[str]:
    """Human-readable validation errors, empty when `data` conforms."""
    validator = Draft202012Validator(load_schema(name, root))
    out = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{where}: {err.message}")
    return out


def validate(name: str, data: object, root: Path | None = None) -> None:
    problems = errors(name, data, root)
    if problems:
        raise SchemaError(f"{name} output invalid: " + "; ".join(problems[:5]))
