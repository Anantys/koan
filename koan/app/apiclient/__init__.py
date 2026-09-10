"""Shared OpenAPI-driven HTTP client contracts."""


class ApiClientError(Exception):
    """Actionable REST client failure safe for user-facing output."""

    @classmethod
    def connection(cls, url: str, detail: str) -> "ApiClientError":
        return cls(
            f"REST API unreachable at {url}: {detail}. "
            "Set api.enabled: true in instance/config.yaml; run make api-token; "
            "start server with make api."
        )


from app.apiclient.client import RestApiClient


__all__ = ["ApiClientError", "RestApiClient"]
