"""The product catalog."""

from dataclasses import dataclass, field

from storefront.catalog.categories import Category
from storefront.errors import UnknownProductError


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: Category
    unit_price_cents: int


@dataclass
class Catalog:
    _products: dict[str, Product] = field(default_factory=dict)

    def add(self, product: Product) -> None:
        if product.unit_price_cents < 0:
            raise ValueError(f"negative price for {product.sku}")
        self._products[product.sku] = product

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise UnknownProductError(sku) from None

    def products(self) -> list[Product]:
        return [self._products[sku] for sku in sorted(self._products)]

    def in_category(self, category: Category) -> list[Product]:
        return [product for product in self.products() if product.category is category]

    def __contains__(self, sku: object) -> bool:
        return sku in self._products

    def __len__(self) -> int:
        return len(self._products)
