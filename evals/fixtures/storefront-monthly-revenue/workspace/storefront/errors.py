"""Errors raised by the storefront package."""


class StorefrontError(Exception):
    """Base class for storefront failures."""


class UnknownProductError(StorefrontError):
    """A product identifier does not exist in the catalog."""


class InvalidOrderError(StorefrontError):
    """An order breaks one of the validation rules."""


class InsufficientStockError(StorefrontError):
    """A reservation asks for more units than a warehouse holds."""
