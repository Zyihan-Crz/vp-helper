"""Resolve a submission's contest without guessing the last selected VP."""
from __future__ import annotations

from pathlib import Path

from .models import VPError
from .storage import read_json


def submission_configuration(source: Path | None = None) -> tuple[Path, dict]:
    current = Path.cwd() / "contest.json"
    beside_source = None
    if source is not None:
        directory = source.resolve().parent
        for parent in (directory, *directory.parents):
            candidate = parent / "contest.json"
            if candidate.exists():
                beside_source = candidate
                break
    if current.exists():
        if beside_source is not None and current.resolve() != beside_source.resolve():
            here = read_json(current)
            there = read_json(beside_source)
            same_target = (isinstance(here, dict) and isinstance(there, dict)
                           and bool(here.get("base_url")) and bool(here.get("handle"))
                           and all(here.get(key) == there.get(key) for key in (
                               "base_url", "handle", "problems", "problem_ids", "offset", "platform")))
            if not same_target:
                raise VPError("The current directory and source file have different contest configurations. "
                              "Run submit from the source file's contest directory.")
        path = current
    elif beside_source is not None:
        path = beside_source
    else:
        raise VPError("No contest.json was found in the current directory or the source file's parent directories. "
                      "Run vp for this contest as the same Linux user, then submit from its prepared contest directory.")
    config = read_json(path)
    if not isinstance(config, dict):
        raise VPError(f"{path} must contain a contest configuration object.")
    return path, config
