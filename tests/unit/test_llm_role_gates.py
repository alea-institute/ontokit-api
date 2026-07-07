"""Tests for per-role LLM access gates — ROLE-01 through ROLE-05."""

from __future__ import annotations

import pytest

from ontokit.services.llm.rate_limiter import RATE_LIMITS
from ontokit.services.llm.role_gates import (
    LLM_ACCESS_ROLES,
    check_llm_access,
    get_role_description,
)


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "suggester"])
def test_llm_access_granted_roles(role: str):
    assert check_llm_access(role) is True


@pytest.mark.parametrize("role", ["viewer", "unknown", ""])
def test_llm_access_denied_roles(role: str):
    assert check_llm_access(role) is False


def test_llm_access_denied_for_none_role():
    assert check_llm_access(None) is False


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "suggester", "viewer"])
def test_anonymous_always_denied_regardless_of_role(role: str):
    """ROLE-05: is_anonymous overrides any role claim."""
    assert check_llm_access(role, is_anonymous=True) is False


def test_access_roles_and_rate_limits_agree():
    """Every access-granting role has a non-zero rate limit and vice versa."""
    for role, limit in RATE_LIMITS.items():
        has_access = role in LLM_ACCESS_ROLES
        assert has_access == (limit is None or limit > 0), (
            f"role {role!r}: access={has_access} but rate limit={limit}"
        )


def test_role_descriptions_match_rate_limits():
    for role in ("owner", "admin", "editor", "suggester", "viewer"):
        desc = get_role_description(role)
        assert desc["daily_limit"] == RATE_LIMITS[role]
        assert desc["can_use_llm"] == check_llm_access(role)


def test_editor_structural_self_merge_defaults_off():
    """ROLE-02/ROLE-03: editors self-merge annotations only; structural is a per-member override."""
    desc = get_role_description("editor")
    assert desc["can_self_merge_annotations"] is True
    assert desc["can_self_merge_structural"] is False


def test_admin_and_owner_self_merge_everything():
    for role in ("owner", "admin"):
        desc = get_role_description(role)
        assert desc["can_self_merge_annotations"] is True
        assert desc["can_self_merge_structural"] is True


def test_unknown_role_gets_zero_capabilities():
    desc = get_role_description("bogus")
    assert desc["can_use_llm"] is False
    assert desc["daily_limit"] == 0
    assert desc["can_self_merge_annotations"] is False
    assert desc["can_self_merge_structural"] is False
