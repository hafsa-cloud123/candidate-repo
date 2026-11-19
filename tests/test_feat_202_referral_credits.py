"""FEAT-202: referral credit ledger.

Client thread 4 (Omar Haddad): the referrer earns 10% (capped at $50, half-up) of
the referee's first invoice once it is fully paid, measured on the pre-tax amount
after discount. One credit per referee, expiring after 365 days. The credit is
applied to the referrer's next issued invoice *after tax* (a payment, not a
discount), oldest expiry first, never below zero, never as cash.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from brewbox.models import InvoiceStatus, Plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def rita(service):
    return service.create_customer("Rita Referrer", "rita@example.com", "RITA-1")


@pytest.fixture
def ravi(service, rita):
    return service.create_customer("Ravi Referee", "ravi@example.com", "RAVI-1", referred_by="RITA-1")


def _subscribe(service, customer, plan="starter", addons=("extra_bag",), start=date(2025, 1, 1), discount=0):
    return service.create_subscription(
        customer["id"], plan_code=plan, addon_codes=list(addons),
        start_date=start, discount_percent=discount,
    )


def _issue(service, sub, year, month, issued_on):
    return service.issue_invoice(sub["id"], year, month, issued_on=issued_on)


def _first_invoice_paid(service, customer, paid_on=date(2025, 1, 10), **subscription_options):
    sub = _subscribe(service, customer, **subscription_options)
    invoice = _issue(service, sub, 2025, 1, date(2025, 1, 1))
    service.record_payment(invoice.id, paid_on=paid_on)
    return invoice


def _add_plan(service, code, price):
    service.repo.plans[code] = Plan(code, code.title(), Decimal(price))


# ---------------------------------------------------------------------------
# Creating customers with a referral code
# ---------------------------------------------------------------------------
def test_referred_by_an_existing_code_is_accepted(service, rita):
    customer = service.create_customer("Ravi", "ravi@example.com", "RAVI-1", referred_by="RITA-1")

    assert customer["referred_by"] == "RITA-1"


def test_referral_codes_are_matched_case_insensitively(service, rita):
    customer = service.create_customer("Ravi", "ravi@example.com", "RAVI-1", referred_by=" rita-1 ")

    assert customer["referred_by"] == "RITA-1"


def test_unknown_referral_code_is_rejected_and_nothing_is_created(service, rita):
    with pytest.raises(ValueError):
        service.create_customer("Ravi", "ravi@example.com", "RAVI-1", referred_by="NOPE-9")

    assert list(service.repo.customers) == [rita["id"]]


@pytest.mark.parametrize("own_code", ["SELF-1", "self-1"])
def test_self_referral_is_rejected(service, own_code):
    with pytest.raises(ValueError):
        service.create_customer("Sam", "sam@example.com", "SELF-1", referred_by=own_code)

    assert service.repo.customers == {}


def test_customer_without_referral_is_still_fine(service):
    customer = service.create_customer("Solo", "solo@example.com", "SOLO-1")

    assert customer["referred_by"] is None


# ---------------------------------------------------------------------------
# When a credit is created
# ---------------------------------------------------------------------------
def test_no_credit_while_the_first_invoice_is_only_issued(service, rita, ravi):
    _issue(service, _subscribe(service, ravi), 2025, 1, date(2025, 1, 1))

    assert service.list_credits(rita["id"]) == []
    assert service.credit_balance(rita["id"]) == Decimal("0.00")


def test_no_credit_for_a_partial_payment(service, rita, ravi):
    invoice = _issue(service, _subscribe(service, ravi), 2025, 1, date(2025, 1, 1))

    service.record_payment(invoice.id, amount="20.00", paid_on=date(2025, 1, 10))

    assert service.list_credits(rita["id"]) == []


def test_credit_is_created_when_the_first_invoice_is_fully_paid(service, rita, ravi):
    invoice = _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))

    (credit,) = service.list_credits(rita["id"])
    assert credit.customer_id == rita["id"]
    assert credit.source_customer_id == ravi["id"]
    assert credit.source_invoice_id == invoice.id
    assert credit.amount == Decimal("2.50")
    assert credit.remaining == Decimal("2.50")
    assert credit.issued_on == date(2025, 1, 10)
    assert credit.expires_on == date(2026, 1, 10)
    assert service.credit_balance(rita["id"], as_of=date(2025, 2, 1)) == Decimal("2.50")


def test_credit_is_created_after_two_part_payments_complete_the_invoice(service, rita, ravi):
    invoice = _issue(service, _subscribe(service, ravi), 2025, 1, date(2025, 1, 1))

    service.record_payment(invoice.id, amount="20.00", paid_on=date(2025, 1, 5))
    assert service.list_credits(rita["id"]) == []
    service.record_payment(invoice.id, paid_on=date(2025, 1, 12))

    (credit,) = service.list_credits(rita["id"])
    assert credit.issued_on == date(2025, 1, 12)


def test_customer_without_a_referrer_creates_no_credit(service, rita):
    solo = service.create_customer("Solo", "solo@example.com", "SOLO-1")

    _first_invoice_paid(service, solo)

    assert service.repo.credits == {}


def test_only_the_first_invoice_earns_a_credit_even_if_paid_second(service, rita, ravi):
    sub = _subscribe(service, ravi)
    _issue(service, sub, 2025, 1, date(2025, 1, 1))  # first invoice stays unpaid
    february = _issue(service, sub, 2025, 2, date(2025, 2, 1))

    service.record_payment(february.id, paid_on=date(2025, 2, 5))

    assert service.list_credits(rita["id"]) == []


def test_one_credit_per_referee(service, rita, ravi):
    sub = _subscribe(service, ravi)
    january = _issue(service, sub, 2025, 1, date(2025, 1, 1))
    february = _issue(service, sub, 2025, 2, date(2025, 2, 1))

    service.record_payment(january.id, paid_on=date(2025, 1, 10))
    service.record_payment(february.id, paid_on=date(2025, 2, 10))

    assert len(service.list_credits(rita["id"])) == 1
    assert service.credit_balance(rita["id"], as_of=date(2025, 3, 1)) == Decimal("2.50")


def test_each_referee_earns_their_own_credit(service, rita, ravi):
    sam = service.create_customer("Sam", "sam@example.com", "SAM-1", referred_by="RITA-1")

    _first_invoice_paid(service, ravi)
    _first_invoice_paid(service, sam)

    assert len(service.list_credits(rita["id"])) == 2
    assert service.credit_balance(rita["id"], as_of=date(2025, 2, 1)) == Decimal("5.00")


# ---------------------------------------------------------------------------
# How much: 10% of (subtotal - discount), capped at $50, half-up
# ---------------------------------------------------------------------------
def test_credit_base_includes_addons(service, rita, ravi):
    _first_invoice_paid(service, ravi, addons=("extra_bag", "grinder"))

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal("3.00")  # 10% of 18.00 + 7.00 + 5.00


def test_credit_base_is_after_discount(service, rita, ravi):
    _first_invoice_paid(service, ravi, addons=(), discount=10)

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal("1.62")  # 10% of (18.00 - 1.80), not 1.80


def test_credit_base_excludes_tax_and_late_fees(service, rita, ravi):
    sub = _subscribe(service, ravi)
    invoice = _issue(service, sub, 2025, 1, date(2025, 1, 1))
    service.apply_late_fee(invoice.id, as_of=date(2025, 2, 1))
    assert invoice.total == Decimal("37.06")  # 25.00 + 2.06 tax + 10.00 fee

    service.record_payment(invoice.id, paid_on=date(2025, 2, 2))

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal("2.50")


def test_credit_base_ignores_how_much_of_the_invoice_was_covered_by_credit(service, rita, ravi):
    # Sam is referred by Ravi and pays first, so Ravi holds a 2.50 credit when his own
    # first invoice is issued. That invoice is part-paid by credit, but Rita's credit
    # is still 10% of its subtotal.
    sam = service.create_customer("Sam", "sam@example.com", "SAM-1", referred_by="RAVI-1")
    _first_invoice_paid(service, sam, paid_on=date(2025, 1, 5))
    sub = _subscribe(service, ravi, start=date(2025, 2, 1))
    invoice = _issue(service, sub, 2025, 2, date(2025, 2, 1))
    assert invoice.credit_applied == Decimal("2.50")
    assert invoice.amount_due == Decimal("24.56")

    service.record_payment(invoice.id, paid_on=date(2025, 2, 10))

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal("2.50")
    assert credit.source_invoice_id == invoice.id


def test_credit_is_rounded_half_up(service, rita, ravi):
    _add_plan(service, "odd", "25.05")

    _first_invoice_paid(service, ravi, plan="odd", addons=())

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal("2.51")  # 2.505 -> half-up (banker's rounding gives 2.50)


@pytest.mark.parametrize(
    "price, expected",
    [("499.00", "49.90"), ("500.00", "50.00"), ("505.00", "50.00"), ("600.00", "50.00")],
    ids=["below-cap", "exactly-cap", "just-above-cap", "far-above-cap"],
)
def test_credit_is_capped_at_fifty_dollars(service, rita, ravi, price, expected):
    # No default plan is large enough to reach the cap, so add a large plan for the test.
    _add_plan(service, "platinum", price)

    _first_invoice_paid(service, ravi, plan="platinum", addons=())

    (credit,) = service.list_credits(rita["id"])
    assert credit.amount == Decimal(expected)


def test_credit_validity_is_365_days_not_twelve_calendar_months(service, rita, ravi):
    sub = _subscribe(service, ravi, start=date(2023, 12, 1))
    invoice = _issue(service, sub, 2023, 12, date(2023, 12, 1))

    service.record_payment(invoice.id, paid_on=date(2023, 12, 1))

    (credit,) = service.list_credits(rita["id"])
    assert credit.expires_on == date(2023, 12, 1) + timedelta(days=365) == date(2024, 11, 30)


# ---------------------------------------------------------------------------
# Applying credits to the referrer's next invoice
# ---------------------------------------------------------------------------
def test_credit_is_applied_after_tax_and_does_not_mark_the_invoice_paid(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    rita_sub = _subscribe(service, rita, start=date(2025, 2, 1))

    invoice = _issue(service, rita_sub, 2025, 2, date(2025, 2, 1))

    assert invoice.tax_total == Decimal("2.06")  # tax on the full 25.00, not on 22.50
    assert invoice.total == Decimal("27.06")
    assert invoice.credit_applied == Decimal("2.50")
    assert invoice.amount_due == Decimal("24.56")
    assert invoice.status == InvoiceStatus.ISSUED
    (credit,) = service.list_credits(rita["id"])
    assert credit.remaining == Decimal("0.00")
    assert service.credit_balance(rita["id"], as_of=date(2025, 2, 1)) == Decimal("0.00")


def test_credit_is_a_payment_not_a_discount(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    rita_sub = _subscribe(service, rita, start=date(2025, 2, 1), discount=20)

    invoice = _issue(service, rita_sub, 2025, 2, date(2025, 2, 1))

    assert invoice.discount_total == Decimal("3.60")
    assert invoice.tax_total == Decimal("1.77")
    assert invoice.total == Decimal("23.17")
    assert invoice.credit_applied == Decimal("2.50")
    assert invoice.amount_due == Decimal("20.67")


def test_invoice_with_credit_can_still_be_paid_for_the_rest(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    invoice = _issue(service, _subscribe(service, rita, start=date(2025, 2, 1)), 2025, 2, date(2025, 2, 1))

    paid = service.record_payment(invoice.id, paid_on=date(2025, 2, 5))

    assert paid.status == InvoiceStatus.PAID
    assert paid.amount_paid == Decimal("24.56")
    assert paid.credit_applied == Decimal("2.50")


def test_late_fee_on_an_invoice_with_credit_raises_the_amount_due(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    invoice = _issue(service, _subscribe(service, rita, start=date(2025, 2, 1)), 2025, 2, date(2025, 2, 1))

    service.apply_late_fee(invoice.id, as_of=date(2025, 3, 1))

    assert invoice.total == Decimal("37.06")
    assert invoice.credit_applied == Decimal("2.50")
    assert invoice.amount_due == Decimal("34.56")


def test_credit_is_not_applied_to_the_invoice_that_generated_it(service, rita, ravi):
    invoice = _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))

    assert invoice.credit_applied == Decimal("0.00")
    assert invoice.amount_due == Decimal("0.00")  # fully paid in cash
    assert service.credit_balance(ravi["id"], as_of=date(2025, 1, 11)) == Decimal("0.00")


def test_credit_belongs_to_the_referrer_only(service, rita, ravi):
    ravi_sub = _subscribe(service, ravi)
    january = _issue(service, ravi_sub, 2025, 1, date(2025, 1, 1))
    service.record_payment(january.id, paid_on=date(2025, 1, 10))

    february = _issue(service, ravi_sub, 2025, 2, date(2025, 2, 1))

    assert february.credit_applied == Decimal("0.00")  # Rita's credit is not Ravi's
    assert service.credit_balance(rita["id"], as_of=date(2025, 2, 1)) == Decimal("2.50")


def test_credit_stays_on_the_account_when_there_is_no_invoice(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))

    # Rita has no subscription: nothing is paid out, the credit simply waits.
    assert service.credit_balance(rita["id"], as_of=date(2025, 6, 1)) == Decimal("2.50")
    (credit,) = service.list_credits(rita["id"])
    assert credit.remaining == Decimal("2.50")


def test_credit_larger_than_the_invoice_leaves_zero_due_and_keeps_the_remainder(service, rita, ravi):
    _add_plan(service, "platinum", "600.00")
    _first_invoice_paid(service, ravi, plan="platinum", addons=(), paid_on=date(2025, 1, 10))
    rita_sub = _subscribe(service, rita, start=date(2025, 2, 1))

    february = _issue(service, rita_sub, 2025, 2, date(2025, 2, 1))

    assert february.total == Decimal("27.06")
    assert february.credit_applied == Decimal("27.06")
    assert february.amount_due == Decimal("0.00")  # never negative
    assert february.status == InvoiceStatus.ISSUED  # zero due is not auto-paid
    assert service.credit_balance(rita["id"], as_of=date(2025, 2, 1)) == Decimal("22.94")

    march = _issue(service, rita_sub, 2025, 3, date(2025, 3, 1))

    assert march.credit_applied == Decimal("22.94")
    assert march.amount_due == Decimal("4.12")
    assert service.credit_balance(rita["id"], as_of=date(2025, 3, 1)) == Decimal("0.00")


def test_oldest_expiring_credit_is_used_first_and_credits_can_be_partly_used(service, rita, ravi):
    sam = service.create_customer("Sam", "sam@example.com", "SAM-1", referred_by="RITA-1")
    # Sam's payment is recorded first but dated later, so his credit expires *later*.
    _first_invoice_paid(service, sam, paid_on=date(2025, 2, 1))
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    later_expiry, earlier_expiry = service.list_credits(rita["id"])
    assert earlier_expiry.expires_on < later_expiry.expires_on
    rita_sub = _subscribe(service, rita, start=date(2025, 3, 31))

    march = _issue(service, rita_sub, 2025, 3, date(2025, 3, 31))  # one-day invoice

    assert march.total == Decimal("0.88")
    assert march.credit_applied == Decimal("0.88")
    assert march.amount_due == Decimal("0.00")
    assert earlier_expiry.remaining == Decimal("1.62")  # partly used, used first
    assert later_expiry.remaining == Decimal("2.50")  # untouched

    april = _issue(service, rita_sub, 2025, 4, date(2025, 4, 1))

    assert april.credit_applied == Decimal("4.12")  # 1.62 + 2.50
    assert april.amount_due == Decimal("22.94")
    assert earlier_expiry.remaining == later_expiry.remaining == Decimal("0.00")


def test_original_amount_is_kept_in_the_ledger_after_use(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    _issue(service, _subscribe(service, rita, start=date(2025, 2, 1)), 2025, 2, date(2025, 2, 1))

    (credit,) = service.list_credits(rita["id"])

    assert credit.amount == Decimal("2.50")
    assert credit.remaining == Decimal("0.00")


def test_issuing_the_same_invoice_again_does_not_use_the_credit_twice(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))
    rita_sub = _subscribe(service, rita, start=date(2025, 2, 1))
    first = _issue(service, rita_sub, 2025, 2, date(2025, 2, 1))

    second = _issue(service, rita_sub, 2025, 2, date(2025, 2, 9))

    assert second is first
    assert second.credit_applied == Decimal("2.50")


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_credit_is_usable_the_day_before_it_expires(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))  # expires 2026-01-10
    rita_sub = _subscribe(service, rita, start=date(2025, 1, 1))

    invoice = _issue(service, rita_sub, 2026, 1, date(2026, 1, 9))

    assert invoice.credit_applied == Decimal("2.50")


def test_credit_is_not_applied_on_the_expiry_date(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))  # expires 2026-01-10
    rita_sub = _subscribe(service, rita, start=date(2025, 1, 1))

    invoice = _issue(service, rita_sub, 2026, 1, date(2026, 1, 10))

    assert invoice.credit_applied == Decimal("0.00")
    assert invoice.amount_due == Decimal("27.06")
    (credit,) = service.list_credits(rita["id"])
    assert credit.remaining == Decimal("2.50")  # still in the ledger, just expired


def test_balance_excludes_expired_credits(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))

    assert service.credit_balance(rita["id"], as_of=date(2026, 1, 9)) == Decimal("2.50")
    assert service.credit_balance(rita["id"], as_of=date(2026, 1, 10)) == Decimal("0.00")
    assert service.credit_balance(rita["id"], as_of=date(2027, 1, 1)) == Decimal("0.00")


def test_balance_defaults_to_the_service_clock(service, rita, ravi):
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))

    assert service.credit_balance(rita["id"]) == Decimal("2.50")  # clock is 2025-03-01


def test_expired_credit_is_skipped_but_a_valid_one_is_still_used(service, rita, ravi):
    sam = service.create_customer("Sam", "sam@example.com", "SAM-1", referred_by="RITA-1")
    _first_invoice_paid(service, ravi, paid_on=date(2025, 1, 10))  # expires 2026-01-10
    _first_invoice_paid(service, sam, paid_on=date(2025, 6, 1))  # expires 2026-06-01
    rita_sub = _subscribe(service, rita, start=date(2025, 1, 1))

    invoice = _issue(service, rita_sub, 2026, 2, date(2026, 2, 1))

    assert invoice.credit_applied == Decimal("2.50")
    expired, valid = service.list_credits(rita["id"])
    assert expired.remaining == Decimal("2.50")
    assert valid.remaining == Decimal("0.00")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
def test_list_credits_is_empty_and_balance_zero_for_everyone_else(service, rita, ravi):
    _first_invoice_paid(service, ravi)

    assert service.list_credits(ravi["id"]) == []
    assert service.credit_balance(ravi["id"], as_of=date(2025, 2, 1)) == Decimal("0.00")
    assert service.list_credits("cus-9999") == []
