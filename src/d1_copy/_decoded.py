"""Narrowing for values read out of decoded JSON, and nothing else.

pyright reads `isinstance(value, dict)` or `list` on a decoded value as a
container of unknown contents, which strict mode refuses to let through. Each
function here makes that one check and returns the value under a type a reader
may assume, or None for a value of another shape. The module is private to the
package: it is plumbing for the API responses the package reads, not part of
what a consumer imports.
"""

from __future__ import annotations

from typing import Any, cast


def json_object(value: object) -> dict[str, Any] | None:
    """`value` as a decoded JSON object, or None when it is anything else.

    A JSON object's keys are always strings.
    """
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def json_array(value: object) -> list[Any] | None:
    """`value` as a decoded JSON array, or None when it is anything else."""
    if not isinstance(value, list):
        return None
    items: list[Any] = [*value]
    return items
