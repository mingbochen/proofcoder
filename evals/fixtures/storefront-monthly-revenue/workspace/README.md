# storefront

A small storefront package: a product catalog, customers, orders, pricing, payments and
sales reports. Money is kept in integer cents throughout.

```text
storefront/
  catalog/     products, categories, name search
  customers/   customers, addresses, loyalty points
  inventory/   stock per warehouse, reservations
  orders/      orders, lines, status, validation, building from the catalog
  pricing/     discounts, promotions, line pricing, tax, shipping
  payments/    payment methods, refunds
  reports/     monthly report, totals, text rendering, CSV export
  utils/       date and text helpers
```

Run the tests with:

```text
python -m unittest discover -s tests
```
