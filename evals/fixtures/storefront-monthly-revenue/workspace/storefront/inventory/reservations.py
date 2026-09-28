"""Reserving stock for an order before it ships."""

from storefront.errors import InsufficientStockError
from storefront.inventory.stock import StockLedger


def reserve(ledger: StockLedger, warehouse: str, sku: str, quantity: int) -> None:
    """Take ``quantity`` units from one warehouse, or fail without taking any."""

    available = ledger.level(warehouse, sku)
    if quantity > available:
        raise InsufficientStockError(f"{sku}: wanted {quantity}, {available} available")
    ledger.take(warehouse, sku, quantity)


def best_warehouse(
    ledger: StockLedger, warehouses: list[str], sku: str, quantity: int
) -> str | None:
    """Return the first warehouse, in the given order, that can serve the whole quantity."""

    for warehouse in warehouses:
        if ledger.level(warehouse, sku) >= quantity:
            return warehouse
    return None
