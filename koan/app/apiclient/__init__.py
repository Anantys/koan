"""Shared OpenAPI-driven HTTP client contracts."""

from app.apiclient.client import DEFAULT_TIMEOUT, RestApiClient
from app.apiclient.errors import ApiClientError

__all__ = ["DEFAULT_TIMEOUT", "ApiClientError", "RestApiClient"]
