"""One parsed provider page; diagnostics are not upstream catalog metadata."""

from dataclasses import dataclass

from ...catalog.records import CatalogEntry


WARNING_CODES = frozenset({"malformed_optional_metadata"})


@dataclass(frozen=True)
class DiscoveryPage:
    entries: tuple[CatalogEntry, ...]
    next_cursor: str | None
    complete: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.entries) > 10_000 or len(self.warnings) > len(WARNING_CODES):
            raise ValueError("invalid_discovery_page")
        if len(set(self.warnings)) != len(self.warnings) or not set(self.warnings) <= WARNING_CODES:
            raise ValueError("invalid_discovery_page")
        if self.complete != (self.next_cursor is None):
            raise ValueError("invalid_discovery_page")
