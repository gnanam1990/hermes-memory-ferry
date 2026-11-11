"""Memory Ferry Hermes memory-provider plugin."""

from .memory_ferry.provider import MemoryFerryProvider


def register(ctx) -> None:
    """Register a fresh provider instance for the active Hermes profile."""
    ctx.register_memory_provider(MemoryFerryProvider())


__all__ = ["MemoryFerryProvider", "register"]

