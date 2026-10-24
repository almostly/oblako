from decimal import Decimal as D

from pipeline import summarise


def event(customer, type_, amount):
    return {"customer": customer, "type": type_, "amount": amount}


def test_orders_are_counted_and_summed():
    events = [
        event("c1", "order", "10.50"),
        event("c1", "order", "4.50"),
        event("c2", "order", "7.00"),
    ]
    assert summarise(events) == [
        {"customer": "c1", "orders": 2, "revenue": D("15.00")},
        {"customer": "c2", "orders": 1, "revenue": D("7.00")},
    ]


def test_refunds_are_subtracted():
    events = [
        event("c1", "order", "20.00"),
        event("c1", "refund", "5.00"),
        event("c3", "refund", "3.00"),
    ]
    assert summarise(events) == [
        {"customer": "c1", "orders": 1, "revenue": D("15.00")},
        {"customer": "c3", "orders": 0, "revenue": D("-3.00")},
    ]


def test_no_events_no_rows():
    assert summarise([]) == []
