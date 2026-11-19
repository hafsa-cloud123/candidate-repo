"""FEAT-201: pause and resume a subscription.

Client thread 3 (Nia Patel): 7 to 90 days inclusive, no overlaps, nothing before
the subscription start, billing skips paused days (same proration as BUG-102),
a fully paused month has no invoice, the overdue check uses *today*, and an early
resume must still leave a pause of at least 7 days.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from brewbox.models import LineKind, PausePeriod, SubscriptionStatus

# The service clock (`today` fixture) is 2025-03-01. Pauses in these tests are
# requested "as of" early January so they are not blocked by overdue invoices.
REQUESTED_ON = date(2025, 1, 2)


def _pause(service, sub, start, end, as_of=REQUESTED_ON):
    return service.pause_subscription(sub["id"], start, end, as_of=as_of)


def _amounts(invoice, kind):
    return [line.amount for line in invoice.lines if line.kind == kind]


def _issue(service, sub, year, month):
    return service.issue_invoice(sub["id"], year, month, issued_on=date(year, month, 1))


# ---------------------------------------------------------------------------
# Length: 7..90 days, inclusive
# ---------------------------------------------------------------------------
def test_seven_day_pause_is_accepted(service, starter_sub):
    pause = _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 16))

    assert pause == PausePeriod(date(2025, 1, 10), date(2025, 1, 16))
    assert pause.days == 7


def test_six_day_pause_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 15))

    assert service.list_pauses(starter_sub["id"]) == []


def test_ninety_day_pause_is_accepted(service, starter_sub):
    pause = _pause(service, starter_sub, date(2025, 1, 1), date(2025, 3, 31))

    assert pause.days == 90


def test_ninety_one_day_pause_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 1, 1), date(2025, 4, 1))

    assert service.list_pauses(starter_sub["id"]) == []


def test_one_day_pause_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 10))


def test_start_after_end_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 1, 20), date(2025, 1, 10))


def test_ten_to_nineteen_january_is_ten_paused_days(service, starter_sub):
    pause = _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))

    assert pause.days == 10


# ---------------------------------------------------------------------------
# Start date, overlap, cancelled, unknown
# ---------------------------------------------------------------------------
def test_pause_before_subscription_start_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2024, 12, 25), date(2025, 1, 5))


def test_pause_starting_on_the_subscription_start_date_is_accepted(service, starter_sub):
    pause = _pause(service, starter_sub, date(2025, 1, 1), date(2025, 1, 7))

    assert pause.start == date(2025, 1, 1)


@pytest.mark.parametrize(
    "start, end",
    [
        (date(2025, 1, 19), date(2025, 1, 25)),  # shares the last day
        (date(2025, 1, 4), date(2025, 1, 10)),  # shares the first day
        (date(2025, 1, 12), date(2025, 1, 18)),  # inside
        (date(2025, 1, 1), date(2025, 1, 31)),  # surrounds
    ],
    ids=["shares-last-day", "shares-first-day", "inside", "surrounds"],
)
def test_overlapping_pause_is_rejected(service, starter_sub, start, end):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))

    with pytest.raises(ValueError):
        _pause(service, starter_sub, start, end)

    assert len(service.list_pauses(starter_sub["id"])) == 1


def test_adjacent_pauses_that_share_no_day_are_accepted(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))
    _pause(service, starter_sub, date(2025, 1, 20), date(2025, 1, 26))
    _pause(service, starter_sub, date(2025, 1, 3), date(2025, 1, 9))

    assert len(service.list_pauses(starter_sub["id"])) == 3


def test_cancelled_subscription_cannot_be_paused(service, starter_sub):
    service.repo.subscriptions[starter_sub["id"]].cancelled_on = date(2025, 2, 1)

    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))


def test_unknown_subscription_is_rejected(service):
    with pytest.raises(ValueError):
        service.pause_subscription("sub-9999", date(2025, 1, 10), date(2025, 1, 19))
    with pytest.raises(ValueError):
        service.resume_subscription("sub-9999", date(2025, 1, 15))
    with pytest.raises(ValueError):
        service.list_pauses("sub-9999")


def test_status_on_reflects_the_pause_window(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))
    sub = service.repo.subscriptions[starter_sub["id"]]

    assert sub.status_on(date(2025, 1, 9)) == SubscriptionStatus.ACTIVE
    assert sub.status_on(date(2025, 1, 10)) == SubscriptionStatus.PAUSED
    assert sub.status_on(date(2025, 1, 19)) == SubscriptionStatus.PAUSED
    assert sub.status_on(date(2025, 1, 20)) == SubscriptionStatus.ACTIVE


# ---------------------------------------------------------------------------
# Billing: paused days are excluded from the plan and every add-on
# ---------------------------------------------------------------------------
def test_partial_month_pause_bills_only_active_days(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))

    invoice = _issue(service, starter_sub, 2025, 1)

    # 31 - 10 paused = 21 active days of 31; billing resumes on 20 January
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("12.19")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("4.74")]
    assert invoice.subtotal == Decimal("16.93")
    assert invoice.tax_total == Decimal("1.40")
    assert invoice.total == Decimal("18.33")


def test_pause_across_a_month_boundary_prorates_both_months(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 28), date(2025, 2, 6))

    january = _issue(service, starter_sub, 2025, 1)
    february = _issue(service, starter_sub, 2025, 2)

    # January loses 28..31 (4 days): 27/31. February loses 1..6 (6 days): 22/28.
    assert _amounts(january, LineKind.PLAN) == [Decimal("15.68")]
    assert _amounts(january, LineKind.ADDON) == [Decimal("6.10")]
    assert january.total == Decimal("23.58")
    assert _amounts(february, LineKind.PLAN) == [Decimal("14.14")]
    assert _amounts(february, LineKind.ADDON) == [Decimal("5.50")]
    assert february.total == Decimal("21.26")


def test_fully_paused_month_has_no_invoice(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 1), date(2025, 1, 31))

    assert _issue(service, starter_sub, 2025, 1) is None
    assert service.repo.invoices == {}
    # the next month is billed normally
    assert _issue(service, starter_sub, 2025, 2).total == Decimal("27.06")


def test_ninety_day_pause_skips_three_months(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 1), date(2025, 3, 31))

    assert [_issue(service, starter_sub, 2025, month) for month in (1, 2, 3)] == [None, None, None]
    assert _issue(service, starter_sub, 2025, 4).total == Decimal("27.06")


def test_pause_covering_a_whole_month_and_part_of_the_next(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 1), date(2025, 2, 6))

    assert _issue(service, starter_sub, 2025, 1) is None
    assert _issue(service, starter_sub, 2025, 2).total == Decimal("21.26")


def test_pause_after_a_mid_month_start_removes_days_from_the_prorated_period(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"], start_date=date(2025, 3, 15)
    )
    _pause(service, sub, date(2025, 3, 20), date(2025, 3, 26))

    invoice = _issue(service, sub, 2025, 3)

    # active: 15..19 (5) + 27..31 (5) = 10 of 31 days
    assert _amounts(invoice, LineKind.PLAN) == [Decimal("5.81")]
    assert _amounts(invoice, LineKind.ADDON) == [Decimal("2.26")]
    assert invoice.total == Decimal("8.74")


def test_discount_applies_to_the_prorated_plan_of_a_paused_month(service, ana):
    sub = service.create_subscription(
        ana["id"], plan_code="starter", addon_codes=["extra_bag"],
        start_date=date(2025, 1, 1), discount_percent=20,
    )
    _pause(service, sub, date(2025, 1, 10), date(2025, 1, 19))

    invoice = _issue(service, sub, 2025, 1)

    assert invoice.discount_total == Decimal("2.44")  # 20% of 12.19, add-on untouched
    assert invoice.tax_total == Decimal("1.20")
    assert invoice.total == Decimal("15.69")


def test_issue_all_invoices_skips_fully_paused_subscriptions(service, ana):
    active = service.create_subscription(ana["id"], plan_code="starter", start_date=date(2025, 1, 1))
    paused = service.create_subscription(ana["id"], plan_code="daily", start_date=date(2025, 1, 1))
    _pause(service, paused, date(2025, 1, 1), date(2025, 1, 31))

    invoices = service.issue_all_invoices(2025, 1)

    assert [invoice.subscription_id for invoice in invoices] == [active["id"]]


def test_existing_invoice_is_not_recalculated_when_a_pause_is_created_later(service, starter_sub):
    january = _issue(service, starter_sub, 2025, 1)
    assert january.total == Decimal("27.06")

    # requested on 5 January: the January invoice is not overdue yet (due 15 January)
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19), as_of=date(2025, 1, 5))

    again = _issue(service, starter_sub, 2025, 1)
    assert again is january
    assert again.total == Decimal("27.06")
    assert len(service.repo.invoices) == 1


# ---------------------------------------------------------------------------
# Overdue guardrail: uses the request date (today), not the pause start
# ---------------------------------------------------------------------------
@pytest.fixture
def january_invoice(service, starter_sub):
    return _issue(service, starter_sub, 2025, 1)  # issued 1 Jan, due 15 Jan


def test_overdue_unpaid_invoice_blocks_a_pause(service, starter_sub, january_invoice):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 19), as_of=date(2025, 1, 16))

    assert service.list_pauses(starter_sub["id"]) == []


def test_invoice_due_today_is_not_overdue(service, starter_sub, january_invoice):
    pause = _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 19), as_of=date(2025, 1, 15))

    assert pause.days == 10


def test_overdue_check_uses_today_even_for_a_future_pause(service, starter_sub, january_invoice):
    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 6, 1), date(2025, 6, 10), as_of=date(2025, 1, 20))


def test_default_as_of_is_the_service_clock(service, starter_sub, january_invoice):
    # service clock is 2025-03-01, so the January invoice is long overdue
    with pytest.raises(ValueError):
        service.pause_subscription(starter_sub["id"], date(2025, 3, 10), date(2025, 3, 20))


def test_paying_the_overdue_invoice_unblocks_the_pause(service, starter_sub, january_invoice):
    service.record_payment(january_invoice.id, paid_on=date(2025, 1, 18))

    pause = _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 19), as_of=date(2025, 1, 20))

    assert pause.days == 10


def test_partially_paid_overdue_invoice_still_blocks_a_pause(service, starter_sub, january_invoice):
    service.record_payment(january_invoice.id, amount="10.00", paid_on=date(2025, 1, 10))

    with pytest.raises(ValueError):
        _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 19), as_of=date(2025, 1, 20))


def test_overdue_invoice_of_another_customer_does_not_block(service, starter_sub, january_invoice):
    bob = service.create_customer("Bob Jones", "bob@example.com", "BOB-1")
    bob_sub = service.create_subscription(bob["id"], plan_code="starter", start_date=date(2025, 1, 1))

    pause = _pause(service, bob_sub, date(2025, 2, 10), date(2025, 2, 19), as_of=date(2025, 1, 20))

    assert pause.days == 10


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
def test_early_resume_shortens_the_pause_and_the_resume_day_is_active(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))

    resumed = service.resume_subscription(starter_sub["id"], date(2025, 1, 20))

    assert resumed == PausePeriod(date(2025, 1, 10), date(2025, 1, 19))
    assert service.list_pauses(starter_sub["id"]) == [resumed]
    sub = service.repo.subscriptions[starter_sub["id"]]
    assert sub.status_on(date(2025, 1, 19)) == SubscriptionStatus.PAUSED
    assert sub.status_on(date(2025, 1, 20)) == SubscriptionStatus.ACTIVE
    assert _issue(service, starter_sub, 2025, 1).total == Decimal("18.33")


def test_early_resume_that_leaves_six_days_is_rejected(service, starter_sub):
    original = _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))

    with pytest.raises(ValueError):
        service.resume_subscription(starter_sub["id"], date(2025, 1, 16))  # 10..15 = 6 days

    assert service.list_pauses(starter_sub["id"]) == [original]


def test_early_resume_that_leaves_exactly_seven_days_is_accepted(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))

    resumed = service.resume_subscription(starter_sub["id"], date(2025, 1, 17))  # 10..16 = 7 days

    assert resumed.days == 7


def test_resuming_on_the_first_pause_day_is_rejected(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))

    with pytest.raises(ValueError):
        service.resume_subscription(starter_sub["id"], date(2025, 1, 10))


@pytest.mark.parametrize("on", [date(2025, 1, 5), date(2025, 1, 26)], ids=["before-pause", "after-pause"])
def test_resume_outside_any_pause_is_rejected(service, starter_sub, on):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))

    with pytest.raises(ValueError):
        service.resume_subscription(starter_sub["id"], on)


def test_resume_without_any_pause_is_rejected(service, starter_sub):
    with pytest.raises(ValueError):
        service.resume_subscription(starter_sub["id"], date(2025, 1, 15))


def test_resume_only_changes_the_pause_that_covers_the_date(service, starter_sub):
    first = _pause(service, starter_sub, date(2025, 1, 3), date(2025, 1, 9))
    _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 25))

    service.resume_subscription(starter_sub["id"], date(2025, 2, 20))

    assert service.list_pauses(starter_sub["id"]) == [
        first,
        PausePeriod(date(2025, 2, 10), date(2025, 2, 19)),
    ]


def test_days_freed_by_an_early_resume_can_be_paused_again(service, starter_sub):
    _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 25))
    service.resume_subscription(starter_sub["id"], date(2025, 1, 20))

    again = _pause(service, starter_sub, date(2025, 1, 20), date(2025, 1, 26))

    assert again.days == 7


# ---------------------------------------------------------------------------
# Audit list
# ---------------------------------------------------------------------------
def test_list_pauses_is_empty_for_a_new_subscription(service, starter_sub):
    assert service.list_pauses(starter_sub["id"]) == []


def test_list_pauses_is_sorted_and_is_a_copy(service, starter_sub):
    late = _pause(service, starter_sub, date(2025, 2, 10), date(2025, 2, 19))
    early = _pause(service, starter_sub, date(2025, 1, 10), date(2025, 1, 19))

    listed = service.list_pauses(starter_sub["id"])
    assert listed == [early, late]

    listed.clear()
    assert len(service.list_pauses(starter_sub["id"])) == 2
