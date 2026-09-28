"""Postal addresses."""

from dataclasses import dataclass

HOME_COUNTRY = "FR"


@dataclass(frozen=True)
class Address:
    street: str
    city: str
    postal_code: str
    country: str

    def one_line(self) -> str:
        return f"{self.street}, {self.postal_code} {self.city}, {self.country}"

    def is_domestic(self) -> bool:
        return self.country.upper() == HOME_COUNTRY
