"""Name search over the catalog."""

from storefront.catalog.products import Catalog, Product
from storefront.utils.text import normalize


def search(catalog: Catalog, query: str) -> list[Product]:
    """Return products whose normalized name contains every word of the query."""

    words = normalize(query).split()
    return [
        product
        for product in catalog.products()
        if all(word in normalize(product.name) for word in words)
    ]
