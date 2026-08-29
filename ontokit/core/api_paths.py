"""Shared API path contracts needed before router dispatch."""

import re

API_V1_PREFIX = "/api/v1"
PROJECTS_PREFIX = "/projects"

ANONYMOUS_SAVE_PATH = "/{project_id}/suggestions/anonymous/sessions/{session_id}/save"
ANONYMOUS_BEACON_PATH = "/{project_id}/suggestions/anonymous/beacon"


def _compile_route_path(template: str) -> re.Pattern[str]:
    marker = "__ONTO_KIT_PATH_SEGMENT__"
    marked = re.sub(r"\{[^{}]+\}", marker, template)
    pattern = re.escape(f"{API_V1_PREFIX}{PROJECTS_PREFIX}{marked}").replace(marker, "[^/]+")
    return re.compile(f"^{pattern}$")


ANONYMOUS_SAVE_PATH_PATTERN = _compile_route_path(ANONYMOUS_SAVE_PATH)
ANONYMOUS_BEACON_PATH_PATTERN = _compile_route_path(ANONYMOUS_BEACON_PATH)
