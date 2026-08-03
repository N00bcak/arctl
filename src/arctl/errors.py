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


class TransientDownstreamError(ProcessError):
    """A recognized external failure that is safe to repeat in a fresh attempt."""

    def __init__(self, stage: str, category: str, detail: str, artifact_path: str):
        super().__init__(detail)
        self.stage = stage
        self.category = category
        self.detail = detail
        self.artifact_path = artifact_path
        self.retries_used = 0
        self.max_retries = 0


class ResearchMiss(ArctlError):
    """A completed research attempt did not produce a usable novel candidate."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
