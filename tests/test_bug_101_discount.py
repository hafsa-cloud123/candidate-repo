"""BUG-101: a promo discount applies to the plan line only and is capped at 30%.

Client thread 1 (Priya Raman): add-ons are never discounted, anything strictly
above 30% is rejected (not clamped), tax is calculated after the discount, and
every component is rounded half-up to cents.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from brewbox.models import LineKind


def _issue_january(service, customer, discount, plan="starter", addons=("extra_bag",)):
    sub = service.create_subscription(
        customer["id"],
        plan_code=plan,
        addon_codes=list(addons),
        start_date=date(2025, 1, 1),
        discount_percent=discount,
    )
    return service.issue_invoice(sub["id"], 2025, 1, issued_on=date(2025, 1, 1))


# ---------------------------------------------------------------------------
# Scope: plan only
# ---------------------------------------------------------------------------
def test_zero_percent_has_no_discount_line(service, ana):
    invoice = _issue_january(service, ana, 0)

    assert invoice.discount_total == Decimal("0.00")
    assert LineKind.DISCOUNT not in [line.kind for line in invoice.lines]
    assert invoice.tax_total == Decimal("2.06")
    assert invoice.total == Decimal("27.06")


def test_discount_line_is_negative_and_total_adds_up(service, ana):
    invoice = _issue_january(service, ana, 20)

    discount_line = next(line for line in invoice.lines if line.kind == LineKind.DISCOUNT)
    assert discount_line.amount == Decimal("-3.60")
    assert invoice.subtotal == Decimal("25.00")
    assert invoice.total == invoice.subtotal - invoice.discount_total + invoice.tax_total


def test_addon_line_is_never_discounted(service, ana):
    invoice = _issue_january(service, ana, 20)

    addon_line = next(line for line in invoice.lines if line.kind == LineKind.ADDON)
    plan_line = next(line for line in invoice.lines if line.kind == LineKind.PLAN)
    assert addon_line.amount == Decimal("7.00")
    assert plan_line.amount == Decimal("18.00")


def test_discount_with_only_a_plan_matches_the_plan_discount(service, ana):
    invoice = _issue_january(service, ana, 10, addons=())

    assert invoice.discount_total == Decimal("1.80")


def test_tax_is_calculated_on_discounted_plan_plus_addons(service, ana):
    invoice = _issue_january(service, ana, 20)

    # (18.00 - 3.60 + 7.00) = 21.40; 21.40 * 8.25% = 1.7655 -> 1.77
    assert invoice.tax_total == Decimal("1.77")
    assert invoice.total == Decimal("23.17")


# ---------------------------------------------------------------------------
# Cap: 0 <= percent <= 30, reject (do not clamp)
# ---------------------------------------------------------------------------
def test_thirty_percent_is_accepted(service, ana):
    invoice = _issue_january(service, ana, 30)

    assert invoice.discount_total == Decimal("5.40")
    assert invoice.tax_total == Decimal("1.62")
    assert invoice.total == Decimal("21.22")


def test_thirty_as_decimal_string_or_float_is_accepted(service, ana):
    for value in (Decimal("30"), "30.0", 30.0):
        sub = service.create_subscription(
            ana["id"], plan_code="starter", start_date=date(2025, 1, 1), discount_percent=value
        )
        assert sub["discount_percent"] == Decimal(str(value))


@pytest.mark.parametrize("bad_discount", ["30.01", 31, 45, 100, "30.1"])
def test_discount_above_thirty_is_rejected_and_nothing_is_created(service, ana, bad_discount):
    with pytest.raises(ValueError):
        service.create_subscription(
            ana["id"],
            plan_code="starter",
            start_date=date(2025, 1, 1),
            discount_percent=bad_discount,
        )

    assert service.repo.subscriptions == {}


@pytest.mark.parametrize("bad_discount", [-1, "-0.01", Decimal("-5")])
def test_negative_discount_is_rejected(service, ana, bad_discount):
    with pytest.raises(ValueError):
        service.create_subscription(
            ana["id"], plan_code="starter", discount_percent=bad_discount
        )

    assert service.repo.subscriptions == {}


@pytest.mark.parametrize("bad_discount", ["abc", "NaN", "Infinity"])
def test_non_numeric_discount_is_rejected_with_value_error(service, ana, bad_discount):
    with pytest.raises(ValueError):
        service.create_subscription(
            ana["id"], plan_code="starter", discount_percent=bad_discount
        )


def test_one_decimal_place_discount_is_valid(service, ana):
    invoice = _issue_january(service, ana, "12.5")

    # 18.00 * 12.5% = 2.25; taxable 18.00 - 2.25 + 7.00 = 22.75; tax 1.876875 -> 1.88
    assert invoice.discount_total == Decimal("2.25")
    assert invoice.tax_total == Decimal("1.88")
    assert invoice.total == Decimal("24.63")


# ---------------------------------------------------------------------------
# Rounding: half-up to cents, before tax
# ---------------------------------------------------------------------------
def test_discount_is_rounded_half_up_to_cents(service, ana):
    # Daily Grind 33.00 * 12.5% = 4.125 exactly. Half-up -> 4.13 (banker's would give 4.12).
    invoice = _issue_january(service, ana, "12.5", plan="daily", addons=())

    assert invoice.discount_total == Decimal("4.13")
    # tax is calculated on the *rounded* discount: (33.00 - 4.13) * 8.25% = 2.381775 -> 2.38
    assert invoice.tax_total == Decimal("2.38")
    assert invoice.total == Decimal("31.25")


def test_issuing_again_does_not_change_the_discounted_invoice(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"],
        start_date=date(2025, 1, 1), discount_percent=20,
    )
    first = service.issue_invoice(sub["id"], 2025, 1, issued_on=date(2025, 1, 1))
    second = service.issue_invoice(sub["id"], 2025, 1, issued_on=date(2025, 1, 9))

    assert first is second
    assert second.total == Decimal("23.17")
