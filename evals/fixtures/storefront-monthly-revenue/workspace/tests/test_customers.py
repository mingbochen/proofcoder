import unittest

from storefront.customers import Address, Customer
from storefront.customers.loyalty import award, points_for, redeem

PARIS = Address("12 rue des Lilas", "Paris", "75011", "FR")
BERLIN = Address("Hauptstrasse 5", "Berlin", "10115", "DE")


class AddressTests(unittest.TestCase):
    def test_one_line(self) -> None:
        self.assertEqual(PARIS.one_line(), "12 rue des Lilas, 75011 Paris, FR")

    def test_domestic(self) -> None:
        self.assertTrue(PARIS.is_domestic())
        self.assertFalse(BERLIN.is_domestic())


class CustomerTests(unittest.TestCase):
    def test_default_address(self) -> None:
        self.assertIsNone(Customer("C1", "Ana", "ana@example.test").default_address())
        customer = Customer("C2", "Ben", "ben@example.test", addresses=[BERLIN, PARIS])
        self.assertEqual(customer.default_address(), BERLIN)

    def test_display_name(self) -> None:
        customer = Customer("C1", "Ana", "ana@example.test")
        self.assertEqual(customer.display_name(), "Ana <ana@example.test>")


class LoyaltyTests(unittest.TestCase):
    def test_partial_euros_earn_nothing(self) -> None:
        self.assertEqual(points_for(1_999), 19)
        self.assertEqual(points_for(-500), 0)

    def test_award_and_redeem(self) -> None:
        customer = Customer("C1", "Ana", "ana@example.test")
        self.assertEqual(award(customer, 4_250), 42)
        redeem(customer, 40)
        self.assertEqual(customer.loyalty_points, 2)
        with self.assertRaises(ValueError):
            redeem(customer, 3)


if __name__ == "__main__":
    unittest.main()
