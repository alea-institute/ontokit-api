"""Generation identity and lifecycle contracts for atomic demo publication."""

from __future__ import annotations

import pytest

from ontokit.services.demo_project_provisioning import (
    DemoProvisioningRefused,
    build_demo_generation_key,
)


def test_generation_key_is_order_independent_and_commit_sensitive() -> None:
    commits = {
        "alea-institute/ontokit-demo-folio": "a" * 40,
        "alea-institute/ontokit-demo-semantic-canon": "b" * 40,
    }

    first = build_demo_generation_key(commits)
    reordered = build_demo_generation_key(dict(reversed(tuple(commits.items()))))
    changed = build_demo_generation_key({**commits, "alea-institute/ontokit-demo-folio": "c" * 40})

    assert first == reordered
    assert first != changed
    assert len(first) == 64


def test_generation_key_refuses_partial_or_non_commit_receipts() -> None:
    with pytest.raises(DemoProvisioningRefused, match="every approved"):
        build_demo_generation_key({"alea-institute/ontokit-demo-folio": "a" * 40})
    with pytest.raises(DemoProvisioningRefused, match="full hexadecimal"):
        build_demo_generation_key(
            {
                "alea-institute/ontokit-demo-folio": "not-a-commit",
                "alea-institute/ontokit-demo-semantic-canon": "b" * 40,
            }
        )
