"""Isolated BiliBot Extension API v1 host."""

from .dispatcher import ExtensionDispatcher
from .registry import ExtensionRegistry

__all__ = ["ExtensionDispatcher", "ExtensionRegistry"]
