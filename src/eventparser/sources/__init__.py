"""Source adapters. Importing this package registers every adapter type."""

from . import events, jobs, linkedin  # noqa: F401
from .base import REGISTRY, Source

__all__ = ["REGISTRY", "Source"]
