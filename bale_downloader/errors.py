"""Errors that are safe to display without leaking credentials or API URLs."""


class FriendlyError(Exception):
    """An expected, user-facing failure."""


class ConfigurationError(FriendlyError):
    """Missing or incompatible deployment configuration."""
