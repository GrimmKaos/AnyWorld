"""Failures shared by inference, validation and auditing."""


class LLMResolutionError(RuntimeError):
    """Raised when the LLM cannot produce valid structured output."""


class LLMBackendUnavailableError(LLMResolutionError):
    """The provider connection failed before a usable model response arrived."""
