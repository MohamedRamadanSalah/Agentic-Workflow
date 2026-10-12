import pytest

from orders import Actor, Order, can_cancel

ALICE = Actor(id="c-1", role="customer")
BOB = Actor(id="c-2", role="customer")
SUPPORT = Actor(id="s-1", role="support")


def order(status: str) -> Order:
    return Order(id="o-1", customer_id="c-1", status=status)


@pytest.mark.parametrize("status", ["PENDING", "PAID"])
def test_customer_can_cancel_early_orders(status):
    assert can_cancel(order(status), ALICE)


def test_customer_cannot_cancel_packed_order():
    assert not can_cancel(order("PACKED"), ALICE)


def test_customer_cannot_cancel_shipped_order():
    assert not can_cancel(order("SHIPPED"), ALICE)


def test_other_customer_cannot_cancel():
    assert not can_cancel(order("PENDING"), BOB)


@pytest.mark.parametrize("status,allowed", [("SHIPPED", True), ("DELIVERED", False)])
def test_support_can_cancel_until_delivered(status, allowed):
    assert can_cancel(order(status), SUPPORT) is allowed


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        can_cancel(order("LOST"), ALICE)
