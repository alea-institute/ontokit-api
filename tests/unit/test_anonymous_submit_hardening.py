"""Anonymous submit input caps and PR-description mention safety (BAIF demo, U5).

Anonymous text lands in a pull-request description that may be mirrored to
GitHub, where ``@name`` pings real people. Anonymous summaries are capped
server-side and their mentions neutralized before rendering.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ontokit.schemas.anonymous_suggestion import AnonymousSubmitRequest
from ontokit.services.suggestion_service import (
    ANONYMOUS_SUMMARY_MAX_LENGTH,
    build_suggestion_pr_description,
    neutralize_mentions,
)

ZWJ = "‍"


def test_anonymous_summary_cap_is_1000() -> None:
    assert ANONYMOUS_SUMMARY_MAX_LENGTH == 1000
    AnonymousSubmitRequest(summary="x" * 1000)
    with pytest.raises(ValidationError):
        AnonymousSubmitRequest(summary="x" * 1001)


def test_anonymous_summary_cap_is_in_openapi_schema() -> None:
    schema = AnonymousSubmitRequest.model_json_schema()
    summary = schema["properties"]["summary"]
    assert {"type": "string", "maxLength": 1000} in summary["anyOf"]


def test_neutralize_mentions_breaks_every_at_sign() -> None:
    text = "cc @octocat and @CatholicOS/maintainers, mail a@b.example"
    out = neutralize_mentions(text)
    assert "@octocat" not in out
    assert f"@{ZWJ}octocat" in out
    assert f"@{ZWJ}CatholicOS/maintainers" in out
    assert out.replace(ZWJ, "") == text


def _description(*, is_anonymous: bool, summary: str, submitter_name: str | None) -> str:
    return build_suggestion_pr_description(
        summary=summary,
        entities=["http://example.org/ont#Foo"],
        changes_count=1,
        is_anonymous=is_anonymous,
        submitter_name=submitter_name,
        user_label="Signed In User",
    )


def test_anonymous_description_neutralizes_summary_and_submitter_mentions() -> None:
    body = _description(is_anonymous=True, summary="ping @octocat", submitter_name="@admin")
    assert "@octocat" not in body
    assert "@admin" not in body
    assert f"ping @{ZWJ}octocat" in body
    assert f"*Submitted by @{ZWJ}admin*" in body


def test_anonymous_description_without_name_is_marked_anonymous() -> None:
    body = _description(is_anonymous=True, summary="plain", submitter_name=None)
    assert body.splitlines()[0] == "plain"
    assert "*Submitted anonymously*" in body
    assert "- http://example.org/ont#Foo" in body


def test_authenticated_description_is_unchanged() -> None:
    body = _description(is_anonymous=False, summary="cc @octocat", submitter_name=None)
    assert body == (
        "cc @octocat\n"
        "\n**Entities modified** (1 changes):\n"
        "- http://example.org/ont#Foo\n"
        "\n*Submitted by Signed In User*"
    )


@pytest.mark.parametrize("field", ["submitter_name", "submitter_email"])
def test_submitter_fields_are_capped_at_column_width(field: str) -> None:
    AnonymousSubmitRequest(**{field: "x" * 255})
    with pytest.raises(ValidationError):
        AnonymousSubmitRequest(**{field: "x" * 256})
