"""Typed artwork failures shared across the native, service, and API boundaries."""


class ArtworkBusyError(RuntimeError):
    """Another bounded native artwork operation is already running."""


class ArtworkStaleIdentityError(RuntimeError):
    """The requested track identity is no longer the current Music.app item."""


class ArtworkTransientError(RuntimeError):
    """A temporary native-process or transport failure prevented extraction."""
