from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from .models import (
    CREDIT_VALID_DAYS,
    LATE_FEE,
    LATE_FEE_GRACE_DAYS,
    MAX_DISCOUNT_PERCENT,
    MAX_PAUSE_DAYS,
    MAX_REFERRAL_CREDIT,
    MIN_PAUSE_DAYS,
    REFERRAL_RATE,
    TAX_RATE,
    CreditEntry,
    Customer,
    Invoice,
    InvoiceStatus,
    LineKind,
    PausePeriod,
    Subscription,
    money,
)
from .repository import InMemoryRepository


class BillingService:
    """Application service for the BrewBox subscription billing assessment.

    All monetary results are rounded to cents with ROUND_HALF_UP.
    """

    def __init__(
        self,
        repository: InMemoryRepository | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.repo = repository or InMemoryRepository()
        self._today = today or date.today

    # ------------------------------------------------------------------
    # Customers and subscriptions
    # ------------------------------------------------------------------
    def create_customer(
        self,
        name: str,
        email: str,
        referral_code: str,
        referred_by: str | None = None,
    ) -> dict:
        email = email.strip().lower()
        referral_code = referral_code.strip().upper()

        if not name.strip():
            raise ValueError("customer name is required")
        if not email:
            raise ValueError("customer email is required")
        if not referral_code:
            raise ValueError("referral code is required")
        if any(c.email == email for c in self.repo.customers.values()):
            raise ValueError("email already registered")
        if self.repo.customer_by_referral_code(referral_code) is not None:
            raise ValueError("referral code already in use")
        if referred_by is not None:
            referred_by = referred_by.strip().upper()
            if referred_by == referral_code:
                raise ValueError("a customer cannot refer themselves")
            if self.repo.customer_by_referral_code(referred_by) is None:
                raise ValueError("unknown referral code")

        customer = Customer(
            id=self.repo.next_id("cus"),
            name=name.strip(),
            email=email,
            referral_code=referral_code,
            referred_by=referred_by,
        )
        self.repo.customers[customer.id] = customer
        return {
            "id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "referral_code": customer.referral_code,
            "referred_by": customer.referred_by,
        }

    def create_subscription(
        self,
        customer_id: str,
        plan_code: str,
        addon_codes: tuple[str, ...] | list[str] = (),
        start_date: date | None = None,
        discount_percent: Decimal | int | str = Decimal("0"),
    ) -> dict:
        if customer_id not in self.repo.customers:
            raise ValueError("customer does not exist")
        if plan_code not in self.repo.plans:
            raise ValueError("unknown plan")
        unknown = [code for code in addon_codes if code not in self.repo.addons]
        if unknown:
            raise ValueError(f"unknown add-on(s): {', '.join(unknown)}")

        discount = self._parse_discount(discount_percent)

        subscription = Subscription(
            id=self.repo.next_id("sub"),
            customer_id=customer_id,
            plan_code=plan_code,
            addon_codes=tuple(addon_codes),
            start_date=start_date or self._today(),
            discount_percent=discount,
        )
        self.repo.subscriptions[subscription.id] = subscription
        return {
            "id": subscription.id,
            "customer_id": subscription.customer_id,
            "plan_code": subscription.plan_code,
            "addon_codes": subscription.addon_codes,
            "start_date": subscription.start_date,
            "discount_percent": subscription.discount_percent,
        }

    # ------------------------------------------------------------------
    # Invoicing
    # ------------------------------------------------------------------
    def issue_invoice(
        self,
        subscription_id: str,
        year: int,
        month: int,
        issued_on: date | None = None,
    ) -> Invoice | None:
        subscription = self.repo.subscriptions.get(subscription_id)
        if subscription is None:
            raise ValueError("subscription does not exist")
        if not 1 <= month <= 12:
            raise ValueError("month must be between 1 and 12")

        period_start, period_end = self._month_bounds(year, month)
        existing = self._find_invoice(subscription_id, year, month)
        if existing is not None:
            return existing

        if period_end < subscription.start_date:
            return None
        if (
            subscription.cancelled_on is not None
            and period_start >= subscription.cancelled_on
        ):
            return None

        active_days = self._active_days(subscription, period_start, period_end)
        if active_days == 0:
            return None

        issued = issued_on or self._today()
        plan = self.repo.plans[subscription.plan_code]

        # Proration (client thread 2): billable calendar days divided by the
        # real number of days in the month. The plan and every add-on use the
        # same ratio and each line is rounded to cents on its own.
        days_in_month = period_end.day
        plan_amount = self._prorate(plan.monthly_price, active_days, days_in_month)
        addon_amounts = [
            (
                self.repo.addons[code],
                self._prorate(
                    self.repo.addons[code].monthly_price, active_days, days_in_month
                ),
            )
            for code in subscription.addon_codes
        ]
        subtotal = money(plan_amount + sum((amount for _, amount in addon_amounts), Decimal("0")))

        # The promo discount applies to the plan fee only (client thread 1):
        # add-ons are never discounted. Tax is calculated after the discount.
        discount_total = money(
            plan_amount * subscription.discount_percent / Decimal("100")
        )
        taxable = money(subtotal - discount_total)
        tax_total = money(taxable * TAX_RATE)

        invoice = Invoice(
            id=self.repo.next_id("inv"),
            customer_id=subscription.customer_id,
            subscription_id=subscription.id,
            period_start=period_start,
            period_end=period_end,
            issued_on=issued,
            due_on=issued + timedelta(days=14),
        )
        invoice.add_line(f"{plan.name} plan", plan_amount, LineKind.PLAN)
        for addon, amount in addon_amounts:
            invoice.add_line(addon.name, amount, LineKind.ADDON)
        if discount_total > 0:
            display_percent = format(subscription.discount_percent.normalize(), "f")
            invoice.add_line(
                f"Discount {display_percent}%", -discount_total, LineKind.DISCOUNT
            )
        invoice.add_line("Sales tax (8.25%)", tax_total, LineKind.TAX)

        invoice.subtotal = subtotal
        invoice.discount_total = discount_total
        invoice.tax_total = tax_total
        invoice.total = money(subtotal - discount_total + tax_total)

        self._apply_available_credits(invoice, issued)
        self.repo.invoices[invoice.id] = invoice
        return invoice

    def issue_all_invoices(self, year: int, month: int) -> list[Invoice]:
        invoices: list[Invoice] = []
        for subscription_id in list(self.repo.subscriptions):
            invoice = self.issue_invoice(subscription_id, year, month)
            if invoice is not None:
                invoices.append(invoice)
        return invoices

    # ------------------------------------------------------------------
    # Payments, late fees, credits
    # ------------------------------------------------------------------
    def record_payment(
        self,
        invoice_id: str,
        amount: Decimal | int | str | None = None,
        paid_on: date | None = None,
    ) -> Invoice:
        invoice = self._invoice(invoice_id)
        if invoice.status == InvoiceStatus.PAID:
            raise ValueError("invoice is already paid")
        if invoice.status == InvoiceStatus.VOID:
            raise ValueError("void invoice cannot be paid")

        due = invoice.amount_due
        paid = money(amount if amount is not None else due)
        if paid <= 0:
            raise ValueError("payment amount must be positive")
        if paid > due:
            raise ValueError("payment exceeds amount due")

        invoice.amount_paid = money(invoice.amount_paid + paid)
        payment_date = paid_on or self._today()
        if invoice.amount_due == 0:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_on = payment_date
            self._award_referral_credit(invoice, payment_date)
        return invoice

    def apply_late_fee(self, invoice_id: str, as_of: date | None = None) -> Invoice:
        invoice = self._invoice(invoice_id)
        day = as_of or self._today()
        if invoice.status != InvoiceStatus.ISSUED:
            return invoice
        if invoice.amount_due <= 0:
            return invoice
        # Five full calendar days of grace after the due date: due + 5 is still
        # on time, the fee starts on due + 6 (client thread 2).
        if day <= invoice.due_on + timedelta(days=LATE_FEE_GRACE_DAYS):
            return invoice
        if invoice.late_fee > 0:
            return invoice

        invoice.add_line("Late payment fee", LATE_FEE, LineKind.LATE_FEE)
        invoice.late_fee = money(LATE_FEE)
        invoice.total = money(invoice.total + LATE_FEE)
        return invoice

    def credit_balance(self, customer_id: str, as_of: date | None = None) -> Decimal:
        """Unused credit that can still be applied on `as_of` (default: today).

        A credit stops counting on its expiry date, so an expired credit is
        excluded from the balance even though it stays in the ledger.
        """
        day = as_of or self._today()
        return money(
            sum(
                (
                    credit.remaining
                    for credit in self.list_credits(customer_id)
                    if credit.expires_on > day
                ),
                Decimal("0.00"),
            )
        )

    def list_credits(self, customer_id: str) -> list[CreditEntry]:
        """Every credit ever earned by this customer, in the order it was created."""
        return [
            credit
            for credit in self.repo.credits.values()
            if credit.customer_id == customer_id
        ]

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------
    def pause_subscription(
        self,
        subscription_id: str,
        start: date,
        end: date,
        as_of: date | None = None,
    ) -> PausePeriod:
        """Pause billing from `start` to `end`; both days are inclusive.

        A pause lasts 7 to 90 days, cannot overlap another pause, cannot start
        before the subscription does, and is refused while the customer has an
        overdue unpaid invoice. "Overdue" is judged on the day the pause is
        requested (`as_of`, defaulting to today), not on the pause start date.
        Invoices that already exist are never recalculated.
        """
        subscription = self._subscription(subscription_id)
        if subscription.cancelled_on is not None:
            raise ValueError("a cancelled subscription cannot be paused")
        if start > end:
            raise ValueError("pause start must not be after pause end")

        pause = PausePeriod(start, end)
        if not MIN_PAUSE_DAYS <= pause.days <= MAX_PAUSE_DAYS:
            raise ValueError(
                f"a pause must last between {MIN_PAUSE_DAYS} and {MAX_PAUSE_DAYS} days"
            )
        if start < subscription.start_date:
            raise ValueError("pause cannot start before the subscription start date")
        if any(pause.overlaps(existing) for existing in subscription.pauses):
            raise ValueError("pause overlaps an existing pause")
        if self._has_overdue_invoice(subscription.customer_id, as_of or self._today()):
            raise ValueError("settle the overdue invoice before pausing")

        subscription.pauses.append(pause)
        return pause

    def resume_subscription(self, subscription_id: str, on: date) -> PausePeriod:
        """Resume early: the pause now ends the day before `on`.

        `on` is active again. The shortened pause must still last at least
        7 days, otherwise the resume is rejected and nothing changes.
        """
        subscription = self._subscription(subscription_id)
        for index, pause in enumerate(subscription.pauses):
            if pause.start <= on <= pause.end:
                break
        else:
            raise ValueError("no pause covers the resume date")

        shortened = PausePeriod(pause.start, on - timedelta(days=1))
        if shortened.days < MIN_PAUSE_DAYS:
            raise ValueError(
                f"the shortened pause must still last at least {MIN_PAUSE_DAYS} days"
            )
        # PausePeriod is immutable, so replace it rather than edit it.
        subscription.pauses[index] = shortened
        return shortened

    def list_pauses(self, subscription_id: str) -> list[PausePeriod]:
        """Pause windows of a subscription in date order (a copy, for audit)."""
        subscription = self._subscription(subscription_id)
        return sorted(subscription.pauses, key=lambda pause: pause.start)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _subscription(self, subscription_id: str) -> Subscription:
        subscription = self.repo.subscriptions.get(subscription_id)
        if subscription is None:
            raise ValueError("subscription does not exist")
        return subscription

    def _has_overdue_invoice(self, customer_id: str, today: date) -> bool:
        """True if an open invoice was due strictly before `today` and still has a balance."""
        return any(
            invoice.status == InvoiceStatus.ISSUED
            and invoice.due_on < today
            and invoice.amount_due > 0
            for invoice in self.repo.invoices_for_customer(customer_id)
        )

    def _invoice(self, invoice_id: str) -> Invoice:
        invoice = self.repo.invoices.get(invoice_id)
        if invoice is None:
            raise ValueError("invoice does not exist")
        return invoice

    @staticmethod
    def _prorate(price: Decimal, active_days: int, days_in_month: int) -> Decimal:
        """Charge `active_days` out of `days_in_month`, rounded half-up to cents.

        A full month is billed at the exact list price. Otherwise the price is
        multiplied by the day count *before* dividing, so no intermediate
        rounding of the ratio can push a value across a half-cent boundary.
        """
        if active_days == days_in_month:
            return money(price)
        return money(price * Decimal(active_days) / Decimal(days_in_month))

    @staticmethod
    def _parse_discount(value: Decimal | int | str) -> Decimal:
        """Return a valid discount percent, or raise ValueError.

        Valid range is 0 <= percent <= 30 (client thread 1). Values above the
        cap are rejected, never silently clamped, so operations sees the typo.
        """
        try:
            discount = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("discount must be a number") from None
        if not discount.is_finite():
            raise ValueError("discount must be a finite number")
        if discount < 0:
            raise ValueError("discount cannot be negative")
        if discount > MAX_DISCOUNT_PERCENT:
            raise ValueError(f"discount cannot be more than {MAX_DISCOUNT_PERCENT}%")
        return discount

    @staticmethod
    def _month_bounds(year: int, month: int) -> tuple[date, date]:
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)

    @staticmethod
    def _active_days(
        subscription: Subscription, period_start: date, period_end: date
    ) -> int:
        """Billable calendar days in the month: from the start date to month end,
        inclusive, minus every paused day that falls inside that range.

        Pauses never overlap each other (checked when they are created), so
        each paused day is subtracted exactly once.
        """
        start = max(period_start, subscription.start_date)
        end = period_end
        if start > end:
            return 0
        days = (end - start).days + 1
        for pause in subscription.pauses:
            paused_from = max(pause.start, start)
            paused_to = min(pause.end, end)
            if paused_from <= paused_to:
                days -= (paused_to - paused_from).days + 1
        return days

    def _find_invoice(
        self, subscription_id: str, year: int, month: int
    ) -> Invoice | None:
        for invoice in self.repo.invoices.values():
            if (
                invoice.subscription_id == subscription_id
                and invoice.period_start.year == year
                and invoice.period_start.month == month
            ):
                return invoice
        return None

    def _apply_available_credits(self, invoice: Invoice, as_of: date) -> None:
        """Pay down a freshly issued invoice with the customer's referral credits.

        Credits behave like a payment made after tax: they never change the
        tax, never push `amount_due` below zero, and never mark the invoice as
        paid. Credits that expire first are used first; an expired credit
        (`expires_on <= as_of`) is skipped.
        """
        still_due = invoice.amount_due
        if still_due <= 0:
            return

        usable = sorted(
            (
                credit
                for credit in self.list_credits(invoice.customer_id)
                if credit.remaining > 0 and credit.expires_on > as_of
            ),
            key=lambda credit: (credit.expires_on, credit.issued_on, credit.id),
        )
        for credit in usable:
            if still_due <= 0:
                break
            used = min(credit.remaining, still_due)
            credit.remaining = money(credit.remaining - used)
            invoice.credit_applied = money(invoice.credit_applied + used)
            still_due = money(still_due - used)

    def _award_referral_credit(self, invoice: Invoice, paid_on: date) -> None:
        """Give the referrer 10% of the referee's first invoice once it is paid in full.

        "First invoice" means the first invoice ever issued to the referee, so
        paying a later invoice while the first is still open earns nothing.
        Each referee earns at most one credit.
        """
        referee = self.repo.customers[invoice.customer_id]
        if referee.referred_by is None:
            return
        referrer = self.repo.customer_by_referral_code(referee.referred_by)
        if referrer is None:
            return

        first_invoice = self.repo.invoices_for_customer(referee.id)[0]
        if invoice.id != first_invoice.id:
            return
        if any(
            credit.source_customer_id == referee.id
            for credit in self.repo.credits.values()
        ):
            return

        # Pre-tax amount after discount: add-ons count, tax and late fees do not.
        base = invoice.subtotal - invoice.discount_total
        amount = min(money(base * REFERRAL_RATE), MAX_REFERRAL_CREDIT)
        if amount <= 0:
            return

        credit = CreditEntry(
            id=self.repo.next_id("crd"),
            customer_id=referrer.id,
            source_customer_id=referee.id,
            source_invoice_id=invoice.id,
            amount=amount,
            remaining=amount,
            issued_on=paid_on,
            expires_on=paid_on + timedelta(days=CREDIT_VALID_DAYS),
            reason="referral",
        )
        self.repo.credits[credit.id] = credit
