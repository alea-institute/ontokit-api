"""Per-role LLM access control and capability mapping.

Per ROLE-01 through ROLE-05:
- ROLE-01: Admin can self-merge all PRs, unlimited LLM calls
- ROLE-02: Editor can self-merge annotation PRs only (by default), 500 LLM calls/day
- ROLE-03: can_self_merge_structural is a per-editor override flag
- ROLE-04: Suggester gets 100 LLM calls/day, routes through suggestion session flow
- ROLE-05: Anonymous/unauthenticated users get no LLM access
"""

from __future__ import annotations

from ontokit.services.llm.rate_limiter import RATE_LIMITS

# Roles that can use LLM features at all
LLM_ACCESS_ROLES: frozenset[str] = frozenset({"owner", "admin", "editor", "suggester"})


def check_llm_access(role: str | None, is_anonymous: bool = False) -> bool:
    """Return True if the user's role grants LLM access.

    Per ROLE-05: anonymous users always return False regardless of role.
    Viewers and unrecognised roles return False.

    Args:
        role: The user's role in the project (e.g. "editor"). May be None.
        is_anonymous: True if the user is not authenticated.

    Returns:
        True if the role grants LLM access; False otherwise.
    """
    if is_anonymous or role is None:
        return False
    return role in LLM_ACCESS_ROLES


def get_role_description(role: str) -> dict:
    """Return a capability descriptor for a given role.

    Describes LLM access, rate limits, and merge permissions.

    Args:
        role: The project role string (e.g. "editor", "admin").

    Returns:
        Dict with keys:
        - can_use_llm (bool)
        - daily_limit (int | None): None means unlimited
        - can_self_merge_annotations (bool)
        - can_self_merge_structural (bool): default value; can be overridden per-member
        - display_label (str)
    """
    _ROLES: dict[str, dict] = {
        "owner": {
            "can_use_llm": True,
            "daily_limit": None,            # unlimited
            "can_self_merge_annotations": True,
            "can_self_merge_structural": True,  # ROLE-01
            "display_label": "Owner",
        },
        "admin": {
            "can_use_llm": True,
            "daily_limit": None,            # unlimited, ROLE-01
            "can_self_merge_annotations": True,
            "can_self_merge_structural": True,  # ROLE-01
            "display_label": "Admin",
        },
        "editor": {
            "can_use_llm": True,
            "daily_limit": RATE_LIMITS["editor"],  # 500, COST-03
            "can_self_merge_annotations": True,    # ROLE-02
            "can_self_merge_structural": False,    # default off; ROLE-03 override per-member
            "display_label": "Editor",
        },
        "suggester": {
            "can_use_llm": True,
            "daily_limit": RATE_LIMITS["suggester"],  # 100, COST-04
            "can_self_merge_annotations": False,
            "can_self_merge_structural": False,
            "display_label": "Suggester",
        },
        "viewer": {
            "can_use_llm": False,
            "daily_limit": 0,
            "can_self_merge_annotations": False,
            "can_self_merge_structural": False,
            "display_label": "Viewer",
        },
    }

    return _ROLES.get(
        role,
        {
            "can_use_llm": False,
            "daily_limit": 0,
            "can_self_merge_annotations": False,
            "can_self_merge_structural": False,
            "display_label": "Unknown",
        },
    )
