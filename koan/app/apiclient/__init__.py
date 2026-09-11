"""Shared OpenAPI-driven HTTP client contracts."""

from app.apiclient.client import RestApiClient
from app.apiclient.errors import ApiClientError

__all__ = ["ApiClientError", "RestApiClient"]
