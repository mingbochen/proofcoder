"""Project-specific exceptions with safe user-facing messages."""


class ProofCoderError(Exception):
    """Base class for recoverable ProofCoder failures."""


class ConfigurationError(ProofCoderError):
    """Raised when process environment configuration is invalid."""


class DeepSeekAPIError(ProofCoderError):
    """Raised when a DeepSeek request or response cannot be handled safely."""


class MessageHistoryError(ProofCoderError):
    """Raised when a message would violate the local chat protocol."""


class ScriptedClientError(ProofCoderError):
    """Raised when an offline scripted response sequence cannot continue."""


class ToolRegistrationError(ProofCoderError):
    """Raised when a tool definition cannot be registered safely."""
