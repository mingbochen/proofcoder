"""Customer records."""

from dataclasses import dataclass, field

from storefront.customers.addresses import Address


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    addresses: list[Address] = field(default_factory=list)
    loyalty_points: int = 0

    def default_address(self) -> Address | None:
        return self.addresses[0] if self.addresses else None

    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"
