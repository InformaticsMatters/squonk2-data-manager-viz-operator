"""Unit tests for the release skill's pure helper functions.

The release process itself (asking the user for a title/channel, checking CI,
creating the GitHub release) is orchestration described in
``.claude/skills/release/SKILL.md``. The *numbering* logic - which is easy to
get subtly wrong and must be deterministic across releases - lives in pure
functions in ``.claude/skills/release/next_release.py`` and is tested here.
"""

import os
import sys

import pytest

# Make the release skill helper importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", ".claude", "skills", "release"),
)

import next_release  # noqa: E402  pylint: disable=wrong-import-position


# --- kubernetes major -------------------------------------------------------


def test_kubernetes_major_is_parsed_from_the_pin() -> None:
    text = "kopf == 1.40.1\nkubernetes == 35.0.0\n"
    assert next_release.kubernetes_major(text) == 35


def test_kubernetes_major_tolerates_whitespace_and_extras() -> None:
    text = "kubernetes==36.1.2  # comment\n"
    assert next_release.kubernetes_major(text) == 36


def test_kubernetes_major_raises_when_absent() -> None:
    with pytest.raises(ValueError):
        next_release.kubernetes_major("kopf == 1.40.1\n")


# --- next tag: pre-releases -------------------------------------------------


def test_first_alpha_uses_n_of_one() -> None:
    tag = next_release.next_tag(
        channel="alpha",
        base_version="35.0.0",
        existing_tags=[],
        kubernetes_major=35,
    )
    assert tag == "35.0.0-alpha.1"


def test_alpha_increments_past_the_highest_existing_alpha() -> None:
    tag = next_release.next_tag(
        channel="alpha",
        base_version="35.0.0",
        existing_tags=["35.0.0-alpha.1", "35.0.0-alpha.2"],
        kubernetes_major=35,
    )
    assert tag == "35.0.0-alpha.3"


def test_beta_numbering_is_independent_of_alpha() -> None:
    tag = next_release.next_tag(
        channel="beta",
        base_version="35.0.0",
        existing_tags=["35.0.0-alpha.1", "35.0.0-alpha.2"],
        kubernetes_major=35,
    )
    assert tag == "35.0.0-beta.1"


def test_release_candidate_increments_its_own_series() -> None:
    tag = next_release.next_tag(
        channel="rc",
        base_version="35.0.0",
        existing_tags=["35.0.0-beta.1", "35.0.0-rc.1"],
        kubernetes_major=35,
    )
    assert tag == "35.0.0-rc.2"


def test_prerelease_numbering_ignores_other_base_versions() -> None:
    tag = next_release.next_tag(
        channel="alpha",
        base_version="35.1.0",
        existing_tags=["35.0.0-alpha.1", "35.0.0-alpha.2"],
        kubernetes_major=35,
    )
    assert tag == "35.1.0-alpha.1"


# --- next tag: final release ------------------------------------------------


def test_final_release_has_no_suffix() -> None:
    tag = next_release.next_tag(
        channel="final",
        base_version="35.0.0",
        existing_tags=["35.0.0-rc.1"],
        kubernetes_major=35,
    )
    assert tag == "35.0.0"


def test_final_release_refuses_to_reuse_an_existing_tag() -> None:
    with pytest.raises(ValueError):
        next_release.next_tag(
            channel="final",
            base_version="35.0.0",
            existing_tags=["35.0.0"],
            kubernetes_major=35,
        )


# --- validation -------------------------------------------------------------


def test_major_must_match_the_kubernetes_package_major() -> None:
    with pytest.raises(ValueError):
        next_release.next_tag(
            channel="alpha",
            base_version="34.0.0",
            existing_tags=[],
            kubernetes_major=35,
        )


def test_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValueError):
        next_release.next_tag(
            channel="gamma",
            base_version="35.0.0",
            existing_tags=[],
            kubernetes_major=35,
        )


def test_malformed_base_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        next_release.next_tag(
            channel="alpha",
            base_version="35.0",
            existing_tags=[],
            kubernetes_major=35,
        )


def test_is_prerelease_classifies_channels() -> None:
    assert next_release.is_prerelease("alpha") is True
    assert next_release.is_prerelease("beta") is True
    assert next_release.is_prerelease("rc") is True
    assert next_release.is_prerelease("final") is False
