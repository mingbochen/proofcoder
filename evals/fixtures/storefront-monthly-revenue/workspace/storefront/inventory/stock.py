"""Stock held per warehouse."""

from dataclasses import dataclass, field


@dataclass
class StockLedger:
    _levels: dict[tuple[str, str], int] = field(default_factory=dict)

    def receive(self, warehouse: str, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise ValueError("received quantity must be positive")
        key = (warehouse, sku)
        self._levels[key] = self._levels.get(key, 0) + quantity

    def level(self, warehouse: str, sku: str) -> int:
        return self._levels.get((warehouse, sku), 0)

    def total(self, sku: str) -> int:
        return sum(quantity for (_, item), quantity in self._levels.items() if item == sku)

    def take(self, warehouse: str, sku: str, quantity: int) -> None:
        self._levels[(warehouse, sku)] = self.level(warehouse, sku) - quantity
