"""Project-specific exceptions with safe user-facing messages."""


class ProofCoderError(Exception):
    """Base class for recoverable ProofCoder failures."""


class ConfigurationError(ProofCoderError):
    """Raised when process environment configuration is invalid."""


class DeepSeekAPIError(ProofCoderError):
    """Raised when a DeepSeek request or response cannot be handled safely."""
