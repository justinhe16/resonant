"""Resonant SDK: the only package extensions depend on."""

from resonant_sdk.events import Event
from resonant_sdk.ids import new_id

__all__ = ["Event", "new_id"]
__version__ = "0.1.0"
