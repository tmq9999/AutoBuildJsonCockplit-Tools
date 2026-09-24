"""Bounded, observational model discovery for explicit provider modes."""

from .parsers import parse_page
from .records import DiscoveryPage

__all__ = ("DiscoveryPage", "parse_page")
