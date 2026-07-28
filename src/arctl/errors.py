"""Domain errors raised by the trusted controller."""


class ArctlError(Exception):
    """Base class for expected arctl failures."""


class ValidationError(ArctlError):
    """Untrusted or user-supplied data failed validation."""


class StateError(ArctlError):
    """A requested state transition is not valid."""


class ProcessError(ArctlError):
    """A managed process could not produce a valid result."""


class StoppedError(ProcessError):
    """A managed process was terminated by a controller stop request."""
