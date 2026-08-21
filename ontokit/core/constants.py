"""OntoKit application-wide constants."""

# Committer identities used by OntoKit for automated commits.
# Any commit with one of these committer emails is considered OntoKit-authored
# and should be excluded from upstream sync processing to prevent feedback loops.
ONTOKIT_COMMITTER_NAME = "OntoKit"
ONTOKIT_COMMITTER_EMAIL = "noreply@ontokit.dev"

ONTOKIT_SYNC_COMMITTER_NAME = "OntoKit Sync"
ONTOKIT_SYNC_COMMITTER_EMAIL = "sync@ontokit.dev"

# Set of all emails used by OntoKit as committer identity.
# Used by the webhook handler to detect and skip self-authored commits.
ONTOKIT_COMMITTER_EMAILS: frozenset[str] = frozenset(
    {ONTOKIT_COMMITTER_EMAIL, ONTOKIT_SYNC_COMMITTER_EMAIL}
)

# Redis pubsub channels for real-time updates.
LINT_UPDATES_CHANNEL = "lint:updates"
NORMALIZATION_UPDATES_CHANNEL = "normalization:updates"
ONTOLOGY_INDEX_UPDATES_CHANNEL = "ontology_index:updates"
QUALITY_UPDATES_CHANNEL = "quality:updates"
REMOTE_SYNC_UPDATES_CHANNEL = "remote_sync:updates"

# Operator-only ARQ control values shared by the worker seam and enqueue command.
PR_PARTY_CREDENTIAL_REWRAP_TASK = "run_pr_party_credential_rewrap_task"
PR_PARTY_CREDENTIAL_REWRAP_DRY_RUN_JOB_ID = "operator:pr-party-credential-rewrap:dry-run"
PR_PARTY_CREDENTIAL_REWRAP_APPLY_JOB_ID = "operator:pr-party-credential-rewrap:apply"
PR_PARTY_CREDENTIAL_REWRAP_CONFIRMATION = "REWRAP-PR-PARTY-CREDENTIALS"
