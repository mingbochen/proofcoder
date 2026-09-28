import unittest

from storefront.catalog import Catalog, Category, Product
from storefront.catalog.categories import parse_category
from storefront.catalog.search import search
from storefront.errors import UnknownProductError


def _catalog() -> Catalog:
    catalog = Catalog()
    catalog.add(Product("BK-001", "Field Guide to Ferns", Category.BOOKS, 2_400))
    catalog.add(Product("BK-002", "Crème brûlée at Home", Category.BOOKS, 1_900))
    catalog.add(Product("TY-200", "Wooden train set", Category.TOYS, 4_200))
    return catalog


class CatalogTests(unittest.TestCase):
    def test_get_and_contains(self) -> None:
        catalog = _catalog()
        self.assertEqual(catalog.get("TY-200").unit_price_cents, 4_200)
        self.assertIn("BK-001", catalog)
        self.assertEqual(len(catalog), 3)

    def test_unknown_product(self) -> None:
        with self.assertRaises(UnknownProductError):
            _catalog().get("XX-999")

    def test_negative_price_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Catalog().add(Product("BAD", "Bad", Category.TOYS, -1))

    def test_category_listing_is_sorted_by_sku(self) -> None:
        skus = [product.sku for product in _catalog().in_category(Category.BOOKS)]
        self.assertEqual(skus, ["BK-001", "BK-002"])

    def test_parse_category(self) -> None:
        self.assertIs(parse_category(" Games "), Category.GAMES)


class SearchTests(unittest.TestCase):
    def test_every_word_must_match(self) -> None:
        self.assertEqual([p.sku for p in search(_catalog(), "field ferns")], ["BK-001"])
        self.assertEqual(search(_catalog(), "field train"), [])

    def test_accents_are_ignored(self) -> None:
        self.assertEqual([p.sku for p in search(_catalog(), "creme brulee")], ["BK-002"])


if __name__ == "__main__":
    unittest.main()
