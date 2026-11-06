"""``getResolvedOptions`` for Glue Python shell jobs, which have no Spark libs."""

from __future__ import annotations

import argparse


def getResolvedOptions(args: list[str], options: list[str]) -> dict[str, str]:  # noqa: N802
    """Return the ``--option value`` arguments named in ``options``, as Glue does.

    Every option named is required, and arguments not named are ignored.
    """
    parser = argparse.ArgumentParser(allow_abbrev=False)
    for option in options:
        parser.add_argument(f"--{option}", required=True)
    parsed, _ = parser.parse_known_args(args[1:])
    return vars(parsed)
