"""BUG-102: first invoices are prorated by real calendar days.

Client thread 2 (Dana Okafor): count the activation day, use the actual number
of days in the month (28/29/30/31), prorate add-ons like the plan, round every
line half-up before tax, and never create a zero-dollar invoice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from brewbox.models import LineKind


def _invoice(service, customer, start, year, month, addons=("extra_bag",), plan="starter", discount=0):
    sub = service.create_subscription(
        customer["id"],
        plan_code=plan,
        addon_codes=list(addons),
        start_date=start,
        discount_percent=discount,
    )
    return service.issue_invoice(sub["id"], year, month, issued_on=date(year, month, 1))


def _amounts(invoice, kind):
    return [line.amount for line in invoice.lines if line.kind == kind]


# ---------------------------------------------------------------------------
# Full month: no proration, no rounding drift
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "year, month",
    [(2025, 1), (2025, 2), (2024, 2), (2025, 4)],
    ids=["31-days", "28-days", "29-days-leap", "30-days"],
)
def test_full_month_bills_exact_list_price(service, ana, year, month):
    invoice = _invoice(service, ana, date(2025, 1, 1) if year == 2025 else date(2024, 1, 1), year, month)

    assert _amounts(invoice, LineKind.PLAN) == [Decimal("18.00")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("7.00")]
    assert invoice.total == Decimal("27.06")


def test_start_on_first_day_of_month_is_a_full_month(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 1), 2025, 3)

    assert _amounts(invoice, LineKind.PLAN) == [Decimal("18.00")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("7.00")]
    assert invoice.total == Decimal("27.06")


# ---------------------------------------------------------------------------
# Partial month: active days / actual days in the month, start day included
# ---------------------------------------------------------------------------
def test_mid_month_start_counts_the_activation_day(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 15), 2025, 3)

    # 15th..31st inclusive = 17 of 31 days
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("9.87")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("3.84")]
    assert invoice.subtotal == Decimal("13.71")
    assert invoice.tax_total == Decimal("1.13")
    assert invoice.total == Decimal("14.84")


def test_invoice_period_still_describes_the_full_month(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 15), 2025, 3)

    assert invoice.period_start == date(2025, 3, 1)
    assert invoice.period_end == date(2025, 3, 31)
    assert invoice.due_on == date(2025, 3, 15)  # issued 1 March + 14 days


def test_thirty_day_month_uses_thirty_as_denominator(service, ana):
    invoice = _invoice(service, ana, date(2025, 4, 10), 2025, 4)

    # 10th..30th inclusive = 21 of 30 days
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("12.60")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("4.90")]
    assert invoice.tax_total == Decimal("1.44")
    assert invoice.total == Decimal("18.94")


def test_leap_february_uses_twenty_nine_days(service, ana):
    invoice = _invoice(service, ana, date(2024, 2, 15), 2024, 2)

    # 15th..29th inclusive = 15 of 29 days
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("9.31")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("3.62")]
    assert invoice.tax_total == Decimal("1.07")
    assert invoice.total == Decimal("14.00")


def test_non_leap_february_uses_twenty_eight_days(service, ana):
    invoice = _invoice(service, ana, date(2025, 2, 15), 2025, 2)

    # 15th..28th inclusive = 14 of 28 days = exactly half
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("9.00")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("3.50")]
    assert invoice.total == Decimal("13.53")


def test_start_on_the_last_day_of_the_month_bills_one_day(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 31), 2025, 3)

    assert _amounts(invoice, LineKind.PLAN) == [Decimal("0.58")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("0.23")]
    assert invoice.tax_total == Decimal("0.07")
    assert invoice.total == Decimal("0.88")


def test_second_day_start_is_not_a_full_month(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 2), 2025, 3)

    # 30 of 31 days
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("17.42")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("6.77")]
    assert invoice.total == Decimal("26.19")


# ---------------------------------------------------------------------------
# Rounding: each line half-up to cents, then tax on the rounded amounts
# ---------------------------------------------------------------------------
def test_each_prorated_line_is_rounded_before_summing_and_before_tax(service, ana):
    invoice = _invoice(
        service, ana, date(2025, 3, 15), 2025, 3, addons=("extra_bag", "grinder", "priority")
    )

    assert _amounts(invoice, LineKind.PLAN) == [Decimal("9.87")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("3.84"), Decimal("2.74"), Decimal("2.19")]
    # Sum of rounded lines is 18.64. Rounding the unrounded sum would give 18.65.
    assert invoice.subtotal == Decimal("18.64")
    assert invoice.tax_total == Decimal("1.54")
    assert invoice.total == Decimal("20.18")


def test_rounding_is_half_up_not_truncation(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 15), 2025, 3, plan="daily", addons=())

    # 33.00 * 17/31 = 18.0967... -> 18.10 (truncation would give 18.09)
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("18.10")]
    assert invoice.total == Decimal("19.59")


# ---------------------------------------------------------------------------
# Zero billable days
# ---------------------------------------------------------------------------
def test_subscription_starting_after_the_month_has_no_invoice(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"], start_date=date(2025, 4, 1)
    )

    assert service.issue_invoice(sub["id"], 2025, 3, issued_on=date(2025, 3, 1)) is None
    assert service.repo.invoices == {}


def test_months_before_the_start_year_also_return_none(service, ana):
    sub = service.create_subscription(ana["id"], plan_code="starter", start_date=date(2025, 6, 10))

    assert service.issue_invoice(sub["id"], 2024, 12, issued_on=date(2024, 12, 1)) is None
    assert service.issue_invoice(sub["id"], 2025, 5, issued_on=date(2025, 5, 1)) is None


def test_first_prorated_invoice_is_idempotent(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"], start_date=date(2025, 3, 15)
    )
    first = service.issue_invoice(sub["id"], 2025, 3, issued_on=date(2025, 3, 1))
    second = service.issue_invoice(sub["id"], 2025, 3, issued_on=date(2025, 3, 20))

    assert first is second
    assert second.total == Decimal("14.84")


def test_month_after_a_prorated_start_is_a_full_month(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"], start_date=date(2025, 3, 15)
    )
    april = service.issue_invoice(sub["id"], 2025, 4, issued_on=date(2025, 4, 1))

    assert _amounts(april, LineKind.PLAN) == [Decimal("18.00")]
    assert april.total == Decimal("27.06")


# ---------------------------------------------------------------------------
# Interaction with BUG-101: discount is taken from the prorated plan amount
# ---------------------------------------------------------------------------
def test_discount_is_calculated_from_the_prorated_plan_amount(service, ana):
    invoice = _invoice(service, ana, date(2025, 3, 15), 2025, 3, discount=20)

    # plan 9.87 -> discount 1.97 (not 20% of 18.00); add-on 3.84 stays undiscounted
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("9.87")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("3.84")]
    assert invoice.discount_total == Decimal("1.97")
    assert invoice.tax_total == Decimal("0.97")
    assert invoice.total == Decimal("12.71")
