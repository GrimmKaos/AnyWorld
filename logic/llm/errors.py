"""Failures shared by inference, validation and auditing."""


class LLMResolutionError(RuntimeError):
    """Raised when the LLM cannot produce valid structured output."""


class LLMOutputTruncatedError(LLMResolutionError):
    """Raised when the provider exhausts the output token limit before completion."""


class LLMBackendUnavailableError(LLMResolutionError):
    """The provider connection failed before a usable model response arrived."""


class SummaryRejectedError(LLMResolutionError):
    """An audit rejected memory and supplied private repair instructions."""

    def __init__(self, corrections: list[str]):
        super().__init__("Summary changed durable facts; original memory retained.")
        self.corrections = corrections
