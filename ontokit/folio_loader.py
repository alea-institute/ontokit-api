"""Load the FOLIO ontology using folio-python."""

import os

from folio import FOLIO


def load_folio() -> FOLIO:
    """Load FOLIO from GitHub (with caching) or local file."""
    source = os.environ.get("FOLIO_SOURCE", "github")
    branch = os.environ.get("FOLIO_BRANCH", "main")

    return FOLIO(
        source_type=source,
        github_repo_owner="alea-institute",
        github_repo_name="folio",
        github_repo_branch=branch,
        use_cache=True,
    )
