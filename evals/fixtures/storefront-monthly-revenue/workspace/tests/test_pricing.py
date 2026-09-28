import unittest

from storefront.catalog import Category
from storefront.customers import Address
from storefront.orders import OrderLine
from storefront.pricing.discounts import DiscountKind, DiscountRule, discount_cents
from storefront.pricing.promotions import parse_rule
from storefront.pricing.shipping import shipping_cents
from storefront.pricing.taxes import tax_cents, tax_rate_for

PARIS = Address("12 rue des Lilas", "Paris", "75011", "FR")
BERLIN = Address("Hauptstrasse 5", "Berlin", "10115", "DE")


class DiscountTests(unittest.TestCase):
    def test_no_rule_takes_nothing(self) -> None:
        self.assertEqual(discount_cents(OrderLine("GM-010", 2, 3_500), None), 0)

    def test_percentage_of_a_single_unit(self) -> None:
        rule = DiscountRule(DiscountKind.PERCENT, 20)
        self.assertEqual(discount_cents(OrderLine("GM-010", 1, 3_500), rule), 700)

    def test_fixed_discount_applies_per_unit(self) -> None:
        rule = DiscountRule(DiscountKind.FIXED_PER_UNIT, 50)
        self.assertEqual(discount_cents(OrderLine("ST-100", 4, 650), rule), 200)

    def test_fixed_discount_never_exceeds_the_price(self) -> None:
        rule = DiscountRule(DiscountKind.FIXED_PER_UNIT, 1_000)
        self.assertEqual(discount_cents(OrderLine("ST-100", 1, 650), rule), 650)

    def test_invalid_rules(self) -> None:
        with self.assertRaises(ValueError):
            DiscountRule(DiscountKind.PERCENT, 101)
        with self.assertRaises(ValueError):
            DiscountRule(DiscountKind.FIXED_PER_UNIT, -1)


class PromotionSpecTests(unittest.TestCase):
    def test_parses_both_kinds(self) -> None:
        self.assertEqual(parse_rule("15%"), DiscountRule(DiscountKind.PERCENT, 15))
        self.assertEqual(parse_rule("0.50 per unit"), DiscountRule(DiscountKind.FIXED_PER_UNIT, 50))

    def test_rejects_unknown_forms(self) -> None:
        with self.assertRaises(ValueError):
            parse_rule("0.50 per box")


class TaxTests(unittest.TestCase):
    def test_books_have_a_reduced_rate(self) -> None:
        self.assertEqual(tax_rate_for(Category.BOOKS), 5)
        self.assertEqual(tax_cents(2_400, Category.BOOKS), 120)

    def test_default_rate(self) -> None:
        self.assertEqual(tax_cents(3_500, Category.GAMES), 700)


class ShippingTests(unittest.TestCase):
    def test_flat_below_the_threshold(self) -> None:
        self.assertEqual(shipping_cents(4_999, PARIS), 495)

    def test_free_from_the_threshold(self) -> None:
        self.assertEqual(shipping_cents(5_000, PARIS), 0)

    def test_international_surcharge(self) -> None:
        self.assertEqual(shipping_cents(5_000, BERLIN), 1_000)
        self.assertEqual(shipping_cents(100, BERLIN), 1_495)


if __name__ == "__main__":
    unittest.main()
