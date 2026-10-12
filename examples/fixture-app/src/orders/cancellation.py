"""Order cancellation rules."""

from __future__ import annotations

from dataclasses import dataclass

STATUS_FLOW = ("PENDING", "PAID", "PACKED", "SHIPPED", "DELIVERED")


@dataclass(frozen=True)
class Order:
    id: str
    customer_id: str
    status: str


@dataclass(frozen=True)
class Actor:
    id: str
    role: str  # "customer" | "support"


def _rank(status: str) -> int:
    if status not in STATUS_FLOW:
        raise ValueError(f"unknown order status: {status}")
    return STATUS_FLOW.index(status)


def can_cancel(order: Order, actor: Actor) -> bool:
    """Return whether ``actor`` may cancel ``order``."""
    if actor.role == "support":
        return _rank(order.status) < _rank("DELIVERED")
    if actor.role != "customer" or actor.id != order.customer_id:
        return False
    return _rank(order.status) < _rank("PACKED")
