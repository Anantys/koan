"""Error type shared by the OpenAPI-driven HTTP client and its front-ends.

Kept in its own module so ``app.apiclient`` can re-export both the error and
:class:`~app.apiclient.client.RestApiClient` from a single import block —
submodules import the error from here, never from the package ``__init__``.
"""


class ApiClientError(Exception):
    """Actionable REST client failure safe for user-facing output."""

    @classmethod
    def connection(cls, url: str, detail: str) -> "ApiClientError":
        return cls(
            f"REST API unreachable at {url}: {detail}. "
            "Set api.enabled: true in instance/config.yaml; run make api-token; "
            "start server with make api."
        )
