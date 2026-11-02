"""Explicit exception hierarchy."""


class BybitDemoError(Exception):
    """Base class for all demo errors."""


class ConfigError(BybitDemoError):
    """Invalid or missing configuration."""


class SchemaError(BybitDemoError):
    """A raw record does not match the provider's documented schema."""

    def __init__(self, message: str, *, source: str = "", line: int | None = None) -> None:
        location = f"{source}:{line}" if line is not None else source
        super().__init__(f"{location}: {message}" if location else message)
        self.source = source
        self.line = line


class BookNotInitializedError(BybitDemoError):
    """A delta was applied before any valid snapshot."""


class ReplayOrderError(BybitDemoError):
    """An input stream is not chronologically ordered, or the clock moved backwards."""


class DownloadError(BybitDemoError):
    """A dataset could not be retrieved."""


class CorruptFileError(BybitDemoError):
    """A raw file cannot be decompressed or is truncated; its contents must not be used."""


class IntegrityError(BybitDemoError):
    """A file's SHA-256 differs from the hash recorded for the same URL in the manifest."""
