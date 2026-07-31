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


class ResearchMiss(ArctlError):
    """A completed research attempt did not produce a usable novel candidate."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
