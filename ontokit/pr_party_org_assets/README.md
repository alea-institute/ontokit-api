# PR Party org assets

Files here are **carried, not run**. They are versioned and tested inside
`ontokit-api` because the service in `ontokit/services/pr_party_qa.py` depends on
their exact shape, but their home is another repository. Nothing in this
directory is on any workflow path in this repo, and adding one here would be a
mistake — the assertions in `tests/unit/test_pr_party_qa.py` are what keep the
copy honest, not GitHub.

## `claude-pr-answers.yml` — the `@claude` answerer

**Destination:** `catholicos/.github`, at `.github/workflows/claude-pr-answers.yml`,
via a fork PR targeting that repository's default branch (`dev`), with a linked
issue.

**What it does.** PR Party posts a reviewer's question as a pull request comment
carrying `@claude`, authored by that reviewer's own PAT. This workflow is what
answers it. The PAT authorship is not a detail: GitHub does not trigger Actions
workflows from events attributed to `GITHUB_TOKEN`, so a question posted by the
app would summon nobody.

**Why it lives in the org repo.** The answerer must work on every repository the
sweep touches, and the reviewers are org members across several of them. Two
install routes, in order of preference:

1. **Org-wide, one copy.** Land the file in `catholicos/.github` and require it
   across the org with a repository ruleset (Settings → Rules → Rulesets → a
   "required workflow"). One file, one place to review, one place to revoke.
2. **Per repository.** Copy the same file into each repository's
   `.github/workflows/`. Works immediately with no org-level configuration, at
   the cost of N copies to keep in step.

**Secret.** The job needs `ANTHROPIC_API_KEY` available to the repositories it
runs in — an organization secret scoped to those repositories, not a per-repo
copy. It has no other secret and needs none.

**Before merging the fork PR, re-read three things.** Each is asserted by
`tests/unit/test_pr_party_qa.py::TestOrgWorkflowAsset`, so a diff that changes
one of them fails the API suite as well:

- The `permissions:` block is exactly `contents: read` + `issues: write`, at both
  the workflow and job level. Naming any scope zeroes the rest, so this block is
  the entire grant. Anything wider — `pull-requests: write`, `id-token: write` —
  hands an agent reading untrusted pull request text a capability it has no use
  for (finding A2).
- The `if:` gate includes `author_association == 'OWNER' || 'MEMBER'`. Without
  it, anyone who can comment on a public pull request can spend the org's tokens
  and publish under the org's name.
- There is **no `actions/checkout`**. Answering a question needs the
  conversation, not the code; checking out a fork's head would put
  attacker-controlled files on a runner holding an API key.

**Answer binding.** The prompt instructs the agent to open its comment with
`> Replying to <comment url>`. That line is the linkage signal PR Party binds the
answer to the question with — the card's Q&A thread never treats "a comment by
the bot" as an answer on the strength of who wrote it (finding C4). An answer
that omits the line still binds when it @-mentions the asker or quotes the
question, but the explicit reference is the deterministic path; keep it in the
prompt.

**Until the fork PR merges,** questions post and appear on the card, and the
thread simply shows them unanswered. That is the documented pre-merge state
(R13) — the feature degrades to "asked on GitHub", which is where the question
was going anyway.
