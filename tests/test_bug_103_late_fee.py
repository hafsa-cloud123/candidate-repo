"""BUG-103: the $10 late fee starts after five *full* days of grace.

Client thread 2 (Dana Okafor): with a due date of 15 January, 20 January is
still on time and the fee starts on 21 January (due date + 6). One fee per
invoice, ever, and it is not taxed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from brewbox.models import InvoiceStatus, LineKind

DUE = date(2025, 1, 15)  # starter_sub issued on 1 January + 14 days


@pytest.fixture
def invoice(service, starter_sub):
    return service.issue_invoice(starter_sub["id"], 2025, 1, issued_on=date(2025, 1, 1))


def _late_fee_lines(invoice):
    return [line for line in invoice.lines if line.kind == LineKind.LATE_FEE]


def test_due_date_is_fourteen_days_after_issue(invoice):
    assert invoice.issued_on == date(2025, 1, 1)
    assert invoice.due_on == DUE


# ---------------------------------------------------------------------------
# Grace window boundary: due + 5 is free, due + 6 is not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "days_after_due",
    [-10, 0, 1, 4, 5],
    ids=["before-due", "due-date", "due+1", "due+4", "due+5"],
)
def test_no_fee_inside_the_grace_period(service, invoice, days_after_due):
    service.apply_late_fee(invoice.id, as_of=DUE + timedelta(days=days_after_due))

    assert invoice.late_fee == Decimal("0.00")
    assert _late_fee_lines(invoice) == []
    assert invoice.total == Decimal("27.06")


@pytest.mark.parametrize("days_after_due", [6, 7, 30, 365], ids=["due+6", "due+7", "due+30", "due+365"])
def test_fee_applies_from_due_plus_six(service, invoice, days_after_due):
    service.apply_late_fee(invoice.id, as_of=DUE + timedelta(days=days_after_due))

    assert invoice.late_fee == Decimal("10.00")
    assert len(_late_fee_lines(invoice)) == 1


def test_example_from_the_client_thread(service, invoice):
    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 20))
    assert invoice.late_fee == Decimal("0.00")

    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))
    assert invoice.late_fee == Decimal("10.00")


def test_grace_period_counts_calendar_days_across_month_end(service, ana):
    sub = service.create_subscription(ana["id"], plan_code="starter", start_date=date(2024, 2, 1))
    leap = service.issue_invoice(sub["id"], 2024, 2, issued_on=date(2024, 2, 12))
    assert leap.due_on == date(2024, 2, 26)

    service.apply_late_fee(leap.id, as_of=date(2024, 3, 2))  # due + 5 (Feb has 29 days)
    assert leap.late_fee == Decimal("0.00")

    service.apply_late_fee(leap.id, as_of=date(2024, 3, 3))  # due + 6
    assert leap.late_fee == Decimal("10.00")


# ---------------------------------------------------------------------------
# What the fee looks like
# ---------------------------------------------------------------------------
def test_fee_is_a_separate_untaxed_line_that_raises_total_and_amount_due(service, invoice):
    tax_before = invoice.tax_total

    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))

    (fee_line,) = _late_fee_lines(invoice)
    assert fee_line.amount == Decimal("10.00")
    assert invoice.tax_total == tax_before == Decimal("2.06")
    assert invoice.total == Decimal("37.06")
    assert invoice.amount_due == Decimal("37.06")
    assert invoice.status == InvoiceStatus.ISSUED


def test_fee_is_applied_once_per_invoice_forever(service, invoice):
    for as_of in (date(2025, 1, 21), date(2025, 1, 22), date(2025, 2, 28), date(2026, 1, 1)):
        service.apply_late_fee(invoice.id, as_of=as_of)

    assert invoice.late_fee == Decimal("10.00")
    assert len(_late_fee_lines(invoice)) == 1
    assert invoice.total == Decimal("37.06")


def test_default_as_of_is_the_service_clock(service, invoice):
    # The service clock is fixed at 2025-03-01 by the `today` fixture: long overdue.
    service.apply_late_fee(invoice.id)

    assert invoice.late_fee == Decimal("10.00")


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def test_paid_invoice_never_gets_a_fee(service, invoice):
    service.record_payment(invoice.id, paid_on=date(2025, 1, 10))

    service.apply_late_fee(invoice.id, as_of=date(2025, 3, 1))

    assert invoice.late_fee == Decimal("0.00")
    assert _late_fee_lines(invoice) == []
    assert invoice.status == InvoiceStatus.PAID


def test_payment_inside_the_grace_window_means_no_fee_on_the_next_run(service, invoice):
    service.record_payment(invoice.id, paid_on=date(2025, 1, 20))

    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))

    assert invoice.late_fee == Decimal("0.00")


def test_partial_payment_keeps_the_invoice_unpaid_and_fee_still_applies(service, invoice):
    service.record_payment(invoice.id, amount="10.00", paid_on=date(2025, 1, 10))
    assert invoice.status == InvoiceStatus.ISSUED

    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))

    assert invoice.late_fee == Decimal("10.00")
    assert invoice.amount_paid == Decimal("10.00")
    assert invoice.amount_due == Decimal("27.06")  # 37.06 - 10.00
    assert invoice.status == InvoiceStatus.ISSUED


def test_partial_payment_inside_grace_period_gets_no_fee(service, invoice):
    service.record_payment(invoice.id, amount="10.00", paid_on=date(2025, 1, 10))

    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 20))

    assert invoice.late_fee == Decimal("0.00")


def test_paying_after_the_fee_keeps_the_fee_and_never_adds_another(service, invoice):
    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))

    service.record_payment(invoice.id, paid_on=date(2025, 1, 22))
    service.apply_late_fee(invoice.id, as_of=date(2025, 2, 15))

    assert invoice.status == InvoiceStatus.PAID
    assert invoice.late_fee == Decimal("10.00")
    assert invoice.total == Decimal("37.06")
    assert invoice.amount_paid == Decimal("37.06")
    assert len(_late_fee_lines(invoice)) == 1


def test_paying_only_the_original_total_leaves_the_fee_outstanding(service, invoice):
    service.apply_late_fee(invoice.id, as_of=date(2025, 1, 21))

    service.record_payment(invoice.id, amount="27.06", paid_on=date(2025, 1, 22))

    assert invoice.status == InvoiceStatus.ISSUED
    assert invoice.amount_due == Decimal("10.00")


def test_void_invoice_never_gets_a_fee(service, invoice):
    invoice.status = InvoiceStatus.VOID

    service.apply_late_fee(invoice.id, as_of=date(2025, 3, 1))

    assert invoice.late_fee == Decimal("0.00")


def test_unknown_invoice_raises(service):
    with pytest.raises(ValueError):
        service.apply_late_fee("inv-9999", as_of=date(2025, 3, 1))
