"""User service for fetching user information from Zitadel."""

from typing import TypedDict

import httpx

from ontokit.core.config import settings


class UserInfo(TypedDict):
    """User information from Zitadel."""

    id: str
    name: str | None
    email: str | None


class UserService:
    """Service for fetching user information from Zitadel."""

    def __init__(self) -> None:
        self._cache: dict[str, UserInfo] = {}

    def _get_service_token(self) -> str | None:
        """
        Get the service account token (PAT) from settings.

        This token has management API access for user lookups.
        """
        return settings.zitadel_service_token or None

    async def get_user_info(self, user_id: str, access_token: str | None = None) -> UserInfo | None:  # noqa: ARG002
        """
        Get user information from Zitadel.

        Uses the Zitadel management API to fetch user details.
        Results are cached in memory for the lifetime of the service instance.

        Args:
            user_id: The Zitadel user ID
            access_token: Unused, kept for backward compatibility

        Returns:
            UserInfo dict with id, name, and email, or None if not found
        """
        # Check cache first
        if user_id in self._cache:
            return self._cache[user_id]

        # Get service token for management API access
        service_token = self._get_service_token()
        if not service_token:
            return None

        # Build the API URL
        base_url = settings.zitadel_internal_url or settings.zitadel_issuer
        url = f"{base_url}/management/v1/users/{user_id}"

        # Build headers
        headers = {
            "Authorization": f"Bearer {service_token}",
            "Content-Type": "application/json",
        }

        # If using internal URL, set Host header
        if settings.zitadel_internal_url:
            from urllib.parse import urlparse

            parsed = urlparse(settings.zitadel_issuer)
            headers["Host"] = parsed.netloc

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)

                if response.status_code == 200:
                    data = response.json()
                    user = data.get("user", {})
                    human = user.get("human", {})
                    profile = human.get("profile", {})
                    email_data = human.get("email", {})

                    # Build display name from profile
                    display_name = profile.get("displayName")
                    if not display_name:
                        first_name = profile.get("firstName", "")
                        last_name = profile.get("lastName", "")
                        display_name = f"{first_name} {last_name}".strip() or None

                    user_info: UserInfo = {
                        "id": user_id,
                        "name": display_name,
                        "email": email_data.get("email"),
                    }

                    # Cache the result
                    self._cache[user_id] = user_info
                    return user_info

                else:
                    return None

        except httpx.HTTPError:
            return None

    async def get_users_info(
        self,
        user_ids: list[str],
        access_token: str | None = None,  # noqa: ARG002
    ) -> dict[str, UserInfo]:
        """
        Get information for multiple users.

        Args:
            user_ids: List of Zitadel user IDs
            access_token: Unused, kept for backward compatibility

        Returns:
            Dict mapping user_id to UserInfo for users that were found
        """
        result: dict[str, UserInfo] = {}

        for user_id in user_ids:
            user_info = await self.get_user_info(user_id)
            if user_info:
                result[user_id] = user_info

        return result

    async def search_users(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[dict[str, str | None]], int]:
        """
        Search Zitadel users by username, email, or display name.

        Uses the Zitadel v2 user search API with an OR query combining
        username, email, and display name filters.

        Args:
            query: The search string (matched as contains, case-insensitive)
            limit: Maximum number of results to return

        Returns:
            Tuple of (list of matching UserInfo dicts, total result count)
        """
        service_token = self._get_service_token()
        if not service_token:
            return [], 0

        base_url = settings.zitadel_internal_url or settings.zitadel_issuer
        url = f"{base_url}/v2/users"

        headers = {
            "Authorization": f"Bearer {service_token}",
            "Content-Type": "application/json",
        }

        if settings.zitadel_internal_url:
            from urllib.parse import urlparse

            parsed = urlparse(settings.zitadel_issuer)
            headers["Host"] = parsed.netloc

        body = {
            "query": {
                "offset": 0,
                "limit": limit,
                "asc": True,
            },
            "queries": [
                {
                    "or_query": {
                        "queries": [
                            {
                                "user_name_query": {
                                    "user_name": query,
                                    "method": "TEXT_QUERY_METHOD_CONTAINS_IGNORE_CASE",
                                },
                            },
                            {
                                "email_query": {
                                    "email_address": query,
                                    "method": "TEXT_QUERY_METHOD_CONTAINS_IGNORE_CASE",
                                },
                            },
                            {
                                "display_name_query": {
                                    "display_name": query,
                                    "method": "TEXT_QUERY_METHOD_CONTAINS_IGNORE_CASE",
                                },
                            },
                        ],
                    },
                },
            ],
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=body, timeout=10.0)

                if response.status_code == 200:
                    data = response.json()
                    total = int(data.get("details", {}).get("totalResult", 0))
                    results: list[dict[str, str | None]] = []

                    for user_data in data.get("result", []):
                        user_id = user_data.get("userId", "")
                        human = user_data.get("human", {})
                        profile = human.get("profile", {})
                        email_data = human.get("email", {})

                        display_name = profile.get("displayName")
                        if not display_name:
                            first = profile.get("givenName", "")
                            last = profile.get("familyName", "")
                            display_name = f"{first} {last}".strip() or None

                        username = user_data.get("preferredLoginName") or user_data.get(
                            "userName", ""
                        )
                        email = email_data.get("email")

                        # Opportunistically populate cache
                        self._cache[user_id] = {
                            "id": user_id,
                            "name": display_name,
                            "email": email,
                        }

                        results.append(
                            {
                                "id": user_id,
                                "username": username,
                                "display_name": display_name,
                                "email": email,
                            }
                        )

                    return results, total

                return [], 0

        except httpx.HTTPError:
            return [], 0

    def clear_cache(self) -> None:
        """Clear the user info cache."""
        self._cache.clear()


# Singleton instance for caching
_user_service: UserService | None = None


def get_user_service() -> UserService:
    """Get the user service singleton."""
    global _user_service
    if _user_service is None:
        _user_service = UserService()
    return _user_service
