#!/usr/bin/env python3
"""Release-numbering helpers for the repository's ``release`` skill.

The release *process* (checking CI, asking for a title/channel, creating the
GitHub release) is orchestration described in ``SKILL.md``. The numbering
*logic* lives here, in small pure functions, so it is deterministic and unit
tested (see ``tests/test_release.py``):

- ``kubernetes_major`` reads the major version the operator is pinned to. The
  release's major **must** match it.
- ``next_tag`` computes the next semver tag for a chosen channel - ``alpha``,
  ``beta`` or ``rc`` pre-releases (``X.Y.Z-channel.N``, first ``N`` is ``1``),
  or a ``final`` full release (``X.Y.Z``).

A small command-line wrapper lets the skill call these from a shell; it reads
the existing tags from ``git`` so the caller does not have to.
"""

import argparse
import re
import subprocess
import sys
from typing import List, Tuple

# The recognised pre-release channels, in the order they are expected to be
# used during a release cycle.
PRERELEASE_CHANNELS: Tuple[str, ...] = ("alpha", "beta", "rc")
# The sentinel channel for a full (non pre-release) release.
FINAL_CHANNEL: str = "final"

_KUBERNETES_PIN = re.compile(r"^\s*kubernetes\s*==\s*(\d+)\.\d+\.\d+", re.MULTILINE)
_BASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def kubernetes_major(requirements_text: str) -> int:
    """Return the major version of the pinned ``kubernetes`` package.

    Reads a ``requirements.txt`` body looking for a line such as
    ``kubernetes == 35.0.0``. A missing/unparsable pin is an error - we must
    never guess the major, as the release tag is built from it.
    """
    match = _KUBERNETES_PIN.search(requirements_text)
    if not match:
        raise ValueError(
            "Could not find a 'kubernetes == X.Y.Z' pin in the requirements"
        )
    return int(match.group(1))


def is_prerelease(channel: str) -> bool:
    """Whether ``channel`` denotes a pre-release (alpha/beta/rc)."""
    return channel in PRERELEASE_CHANNELS


def _parse_base_version(base_version: str) -> Tuple[int, int, int]:
    """Parse and validate an ``X.Y.Z`` base version, returning its components."""
    match = _BASE_VERSION.match(base_version)
    if not match:
        raise ValueError(
            f"Base version '{base_version}' is not a 'MAJOR.MINOR.PATCH' string"
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_tag(
    *,
    channel: str,
    base_version: str,
    existing_tags: List[str],
    kubernetes_major: int,  # pylint: disable=redefined-outer-name
) -> str:
    """Compute the next release tag for ``channel`` against ``base_version``.

    ``channel`` is one of ``alpha``/``beta``/``rc`` (a pre-release) or
    ``final`` (a full release). For a pre-release the result is
    ``{base}-{channel}.{N}`` where ``N`` is one greater than the highest
    existing ``N`` for the same base and channel (``1`` if none exist). For a
    ``final`` release the result is the bare ``{base}``.

    Raises ``ValueError`` when the channel is unknown, the base version is
    malformed, the major does not match ``kubernetes_major``, or the computed
    tag already exists.
    """
    if channel != FINAL_CHANNEL and channel not in PRERELEASE_CHANNELS:
        recognised = ", ".join((*PRERELEASE_CHANNELS, FINAL_CHANNEL))
        raise ValueError(f"Unknown channel '{channel}'; expected one of: {recognised}")

    major, _, _ = _parse_base_version(base_version)
    if major != kubernetes_major:
        raise ValueError(
            f"Release major ({major}) must match the kubernetes package major "
            f"({kubernetes_major})"
        )

    if channel == FINAL_CHANNEL:
        if base_version in existing_tags:
            raise ValueError(f"Tag '{base_version}' already exists")
        return base_version

    prefix = f"{base_version}-{channel}."
    highest = 0
    for tag in existing_tags:
        if tag.startswith(prefix):
            suffix = tag[len(prefix) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1}"


def _git_tags() -> List[str]:
    """Return the repository's existing tags (one per line from ``git tag``)."""
    result = subprocess.run(
        ["git", "tag", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_major = sub.add_parser("major", help="print the pinned kubernetes major")
    p_major.add_argument("requirements", help="path to requirements.txt")

    p_next = sub.add_parser("next", help="print the next release tag")
    p_next.add_argument(
        "--channel",
        required=True,
        choices=(*PRERELEASE_CHANNELS, FINAL_CHANNEL),
    )
    p_next.add_argument(
        "--requirements",
        required=True,
        help="path to requirements.txt (provides the major version)",
    )
    p_next.add_argument(
        "--base",
        default=None,
        help="base 'MAJOR.MINOR.PATCH' version (default '{major}.0.0')",
    )

    args = parser.parse_args(argv)

    if args.command == "major":
        with open(args.requirements, encoding="utf-8") as requirements:
            print(kubernetes_major(requirements.read()))
        return 0

    with open(args.requirements, encoding="utf-8") as requirements:
        major = kubernetes_major(requirements.read())
    base_version = args.base or f"{major}.0.0"
    print(
        next_tag(
            channel=args.channel,
            base_version=base_version,
            existing_tags=_git_tags(),
            kubernetes_major=major,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
