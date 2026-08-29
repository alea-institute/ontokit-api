"""Tests for commit-authoring identity (U9).

Git history is permanent and, once mirrored, public. The load-bearing claim
here is negative: there is no input combination for which a contributor's real
email address ends up in a commit.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ontokit.schemas.user_settings import CommitIdentityUpdate
from ontokit.services.commit_identity import (
    SLUG_FALLBACK,
    CommitIdentityService,
    anonymous_alias,
    get_commit_identity_service,
    noreply_alias,
    slugify_display_name,
    stable_user_suffix,
)

REAL_EMAIL = "maria.gonzalez@parish.example.org"

# RFC-5322-ish: a local part of safe characters, then a domain with a dot.
ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _db(preference: object = None) -> AsyncMock:
    db = AsyncMock()
    db.add = Mock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = preference
    db.execute = AsyncMock(return_value=result)
    return db


def _preference(
    *,
    commit_email: str | None = None,
    verified: bool = False,
    opted_in: bool = False,
) -> MagicMock:
    pref = MagicMock()
    pref.commit_email = commit_email
    pref.commit_email_verified = verified
    pref.use_verified_email = opted_in
    return pref


class TestSlugifyDisplayName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Maria Gonzalez", "maria-gonzalez"),
            ("María González", "maria-gonzalez"),
            ("Fr. John D'Orazio", "fr-john-d-orazio"),
            ("maria.gonzalez+ontokit", "maria-gonzalez-ontokit"),
            ("  Padded  Name  ", "padded-name"),
            ("UPPER", "upper"),
        ],
    )
    def test_folds_to_a_safe_slug(self, name: str, expected: str) -> None:
        assert slugify_display_name(name) == expected

    @pytest.mark.parametrize("name", [None, "", "   ", "日本語", "🎉🎉", "---", "..."])
    def test_unsluggable_names_fall_back(self, name: str | None) -> None:
        """An empty local part would produce a malformed address."""
        assert slugify_display_name(name) == SLUG_FALLBACK

    def test_long_names_are_capped(self) -> None:
        slug = slugify_display_name("A" * 200)
        assert len(slug) <= 32

    def test_cap_never_leaves_a_trailing_hyphen(self) -> None:
        slug = slugify_display_name("a" * 32 + " b")
        assert not slug.endswith("-")


class TestStableUserSuffix:
    def test_is_eight_hex_characters(self) -> None:
        suffix = stable_user_suffix("user-1")
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_is_stable_across_calls(self) -> None:
        assert stable_user_suffix("user-1") == stable_user_suffix("user-1")

    def test_differs_between_users(self) -> None:
        assert stable_user_suffix("user-1") != stable_user_suffix("user-2")

    def test_is_salted_with_the_secret_key(self) -> None:
        """Not a lookup key an outsider can recompute from a guessed user ID."""
        first = stable_user_suffix("user-1")
        with patch("ontokit.services.commit_identity.settings") as mock_settings:
            mock_settings.secret_key = "a-different-secret"
            second = stable_user_suffix("user-1")
        assert first != second


class TestNoreplyAlias:
    def test_shape(self) -> None:
        """Covers AE6."""
        alias = noreply_alias("user-1", "Maria Gonzalez")
        assert alias.startswith("maria-gonzalez-")
        assert alias.endswith("@users.noreply.ontokit.local")
        assert ADDRESS.match(alias)

    def test_same_display_name_different_users_do_not_collide(self) -> None:
        assert noreply_alias("user-1", "Maria Gonzalez") != noreply_alias(
            "user-2", "Maria Gonzalez"
        )

    def test_domain_is_configurable(self) -> None:
        with patch("ontokit.services.commit_identity.settings") as mock_settings:
            mock_settings.secret_key = "k"
            mock_settings.commit_noreply_domain = "users.noreply.catholicos.org"
            assert noreply_alias("user-1", "Maria").endswith("@users.noreply.catholicos.org")

    def test_non_ascii_name_still_yields_a_valid_address(self) -> None:
        assert ADDRESS.match(noreply_alias("user-1", "日本語の名前"))

    def test_anonymous_alias_shape(self) -> None:
        alias = anonymous_alias("s_abc12345")
        assert alias.startswith("anonymous-")
        assert ADDRESS.match(alias)


class TestResolve:
    async def test_default_contributor_gets_the_alias(self) -> None:
        """Covers AE6: no real email, no opt-in row."""
        service = CommitIdentityService(_db(None))
        name, email = await service.resolve("user-1", "Maria Gonzalez")
        assert name == "Maria Gonzalez"
        assert email == noreply_alias("user-1", "Maria Gonzalez")
        assert REAL_EMAIL not in email

    async def test_opted_in_verified_address_is_used(self) -> None:
        """Covers AE6: the power user attributes natively on GitHub."""
        pref = _preference(
            commit_email="1234+maria@users.noreply.github.com", verified=True, opted_in=True
        )
        service = CommitIdentityService(_db(pref))
        _, email = await service.resolve("user-1", "Maria Gonzalez")
        assert email == "1234+maria@users.noreply.github.com"

    async def test_unverified_address_falls_back_to_the_alias(self) -> None:
        pref = _preference(commit_email=REAL_EMAIL, verified=False, opted_in=True)
        service = CommitIdentityService(_db(pref))
        _, email = await service.resolve("user-1", "Maria Gonzalez")
        assert email != REAL_EMAIL
        assert email == noreply_alias("user-1", "Maria Gonzalez")

    async def test_verified_but_not_opted_in_falls_back_to_the_alias(self) -> None:
        pref = _preference(commit_email=REAL_EMAIL, verified=True, opted_in=False)
        service = CommitIdentityService(_db(pref))
        _, email = await service.resolve("user-1", "Maria Gonzalez")
        assert email != REAL_EMAIL

    async def test_opted_in_with_no_address_falls_back_to_the_alias(self) -> None:
        pref = _preference(commit_email=None, verified=True, opted_in=True)
        service = CommitIdentityService(_db(pref))
        _, email = await service.resolve("user-1", "Maria")
        assert email == noreply_alias("user-1", "Maria")

    async def test_missing_display_name_still_yields_a_name(self) -> None:
        service = CommitIdentityService(_db(None))
        name, email = await service.resolve("user-1", None)
        assert name == "Contributor"
        assert ADDRESS.match(email)

    async def test_anonymous_session_never_uses_the_supplied_credit_email(self) -> None:
        """The credit name the submitter typed is used for the NAME only."""
        service = CommitIdentityService(_db(None))
        name, email = await service.resolve(
            "anonymous-abc",
            "A Parish Volunteer",
            is_anonymous=True,
            session_id="s_abc12345",
        )
        assert name == "A Parish Volunteer"
        assert email == anonymous_alias("s_abc12345")

    async def test_anonymous_never_reads_the_preference_table(self) -> None:
        db = _db(_preference(commit_email=REAL_EMAIL, verified=True, opted_in=True))
        service = CommitIdentityService(db)
        _, email = await service.resolve(
            "anonymous-abc", "Volunteer", is_anonymous=True, session_id="s_1"
        )
        assert email != REAL_EMAIL
        db.execute.assert_not_awaited()

    @pytest.mark.parametrize(
        "pref",
        [
            None,
            _preference(commit_email=REAL_EMAIL, verified=False, opted_in=False),
            _preference(commit_email=REAL_EMAIL, verified=False, opted_in=True),
            _preference(commit_email=REAL_EMAIL, verified=True, opted_in=False),
        ],
    )
    async def test_real_address_never_leaks_for_any_input(self, pref: object) -> None:
        """The load-bearing negative claim of this unit."""
        service = CommitIdentityService(_db(pref))
        _, email = await service.resolve("user-1", "Maria Gonzalez")
        assert email != REAL_EMAIL


class TestSetPreference:
    def test_patch_schema_distinguishes_explicit_null_from_omission(self) -> None:
        assert CommitIdentityUpdate(commit_email=None).model_dump(exclude_unset=True) == {
            "commit_email": None
        }
        assert CommitIdentityUpdate().model_dump(exclude_unset=True) == {}

    async def test_creates_a_row_when_absent(self) -> None:
        db = _db(None)
        service = CommitIdentityService(db)
        pref = await service.set_preference("user-1", commit_email="x@example.com")
        db.add.assert_called_once()
        assert pref.commit_email == "x@example.com"

    async def test_changing_the_address_resets_verification(self) -> None:
        """Re-pointing the preference is exactly when someone would try this."""
        existing = _preference(commit_email="old@example.com", verified=True, opted_in=True)
        service = CommitIdentityService(_db(existing))
        pref = await service.set_preference("user-1", commit_email="new@example.com")
        assert pref.commit_email == "new@example.com"
        assert pref.commit_email_verified is False

    async def test_toggling_opt_in_alone_preserves_verification(self) -> None:
        existing = _preference(commit_email="a@example.com", verified=True, opted_in=False)
        service = CommitIdentityService(_db(existing))
        pref = await service.set_preference("user-1", use_verified_email=True)
        assert pref.use_verified_email is True
        assert pref.commit_email_verified is True

    async def test_explicit_null_clears_address_verification_and_opt_in(self) -> None:
        existing = _preference(commit_email="a@example.com", verified=True, opted_in=True)
        service = CommitIdentityService(_db(existing))
        pref = await service.set_preference("user-1", commit_email=None)
        assert pref.commit_email is None
        assert pref.commit_email_verified is False
        assert pref.use_verified_email is False

    async def test_omitting_address_preserves_it(self) -> None:
        existing = _preference(commit_email="a@example.com", verified=True, opted_in=True)
        service = CommitIdentityService(_db(existing))
        pref = await service.set_preference("user-1", use_verified_email=False)
        assert pref.commit_email == "a@example.com"
        assert pref.commit_email_verified is True


class TestFactory:
    def test_returns_a_bound_service(self) -> None:
        db = _db(None)
        service = get_commit_identity_service(db)
        assert isinstance(service, CommitIdentityService)
        assert service.db is db
