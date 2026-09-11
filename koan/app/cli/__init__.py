"""Public contracts shared by the Kōan REST CLI."""

EXIT_OK = 0
EXIT_LOCAL = 1
EXIT_AUTH = 2
EXIT_NOT_FOUND = 3
EXIT_SERVER = 4


class CliError(Exception):
    """A local CLI or configuration error safe to print to stderr."""


__all__ = [
    "CliError",
    "EXIT_AUTH",
    "EXIT_LOCAL",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_SERVER",
]
