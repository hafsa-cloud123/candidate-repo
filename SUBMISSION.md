# Submission

## Repository

- Remote URL: TODO (paste the public GitHub repository URL here)
- Default branch: `main`

## Pull requests

| Ticket | Branch | PR link | Key commits |
|---|---|---|---|
| BUG-101 | `bugfix/BUG-101-discount-scope` | TODO | <!--h:BUG-101-->`f695f4c`, `86c3866`<!--/h--> |
| BUG-102 | `bugfix/BUG-102-calendar-proration` | TODO | <!--h:BUG-102-->`b665351`, `25a8e9e`<!--/h--> |
| BUG-103 | `bugfix/BUG-103-late-fee-grace` | TODO | <!--h:BUG-103-->`572bf06`, `93648f0`<!--/h--> |
| FEAT-201 | `feature/FEAT-201-pause-resume` | TODO | <!--h:FEAT-201-->`ceb6f4b`, `6470013`<!--/h--> |
| FEAT-202 | `feature/FEAT-202-referral-credits` | TODO | <!--h:FEAT-202-->`a982b81`, `7879b0d`<!--/h--> |

Each ticket has a reproduction/specification `test(...)` commit followed by the
`fix(...)`/`feat(...)` commit. Tickets were merged into `main` in the order above
(BUG-102 builds on the discount fix, FEAT-201 on the proration fix).

## Test evidence

- Command: `pytest`
- Result: `162 passed` (the 4 tests that failed in the starter now pass; the
  integrity-bubble tests and every original test are untouched)
- Tests added: 148 new tests in five new files, one per ticket
  (`tests/test_bug_101_discount.py`, `tests/test_bug_102_proration.py`,
  `tests/test_bug_103_late_fee.py`, `tests/test_feat_201_pause_resume.py`,
  `tests/test_feat_202_referral_credits.py`). No existing test or protected file
  was edited.

## Design notes

### BUG-101 — discount scope

- The discount is `money(plan_amount * percent / 100)`: it uses the (prorated) plan
  line only, so add-ons are never discounted. Tax is still calculated on
  `plan - discount + add-ons`.
- `create_subscription` validates `0 <= percent <= 30` through `_parse_discount`.
  Values above 30 are **rejected**, not clamped. `NaN`, `Infinity` and non-numeric
  input also raise `ValueError` (instead of leaking `decimal.InvalidOperation`).
- Decision: more than one decimal place (for example 12.55) is accepted because the
  client thread only says one decimal place is allowed, not that more is forbidden.
  The money amount is rounded half-up anyway.

### BUG-102 — proration

- Billable days = days from the start date to the end of the month, inclusive
  (the activation day counts). The denominator is the real month length
  (`period_end.day`), so February is 28/29 and not a flat 30.
- `_prorate` multiplies the price by the day count and *then* divides, so no rounded
  intermediate ratio can push a value over a half-cent boundary. A full month bills
  the exact list price. The plan and every add-on are prorated and rounded
  separately, then tax is calculated on the rounded lines (15 March: 9.87 + 3.84,
  tax 1.13, total 14.84).
- Zero billable days returns `None` and stores nothing.

### BUG-103 — late fee

- Off-by-one: the fee used to start on due + 5. It now starts on due + 6
  (`day <= due_on + LATE_FEE_GRACE_DAYS` means no fee). The existing guards (paid,
  void, one fee per invoice, untaxed) were already right and are now covered by tests.

### FEAT-201 — pause/resume

- A pause is a stored inclusive window. `_active_days` subtracts every paused day that
  falls inside the billed month, so partial, cross-month and multi-month pauses all
  reuse the BUG-102 ratio. A fully paused month yields no invoice. Existing invoices
  are never recalculated.
- Validation: 7 to 90 days, `start <= end`, not before the subscription start, no
  overlap (a shared day counts), not cancelled (`cancelled_on` set), no overdue
  invoice. "Overdue" is judged on the request date (`as_of`, default today), for any
  `ISSUED` invoice of the same customer that is due strictly before that date and
  still has a balance.
- Decision: a pause that starts in the past is not rejected. The acceptance criteria do
  not list it, and rejecting it would make historical billing impossible to test.
- Resume: finds the pause that contains the resume date (otherwise `ValueError`), ends
  it the day before, and rejects the resume if fewer than 7 days remain (this includes
  resuming on the first pause day). `PausePeriod` is immutable, so it is replaced.

### FEAT-202 — referral credits

- `create_customer` rejects unknown and self referral codes (case-insensitive) before
  anything is stored.
- A credit is created in `_award_referral_credit` when the referee's **first issued
  invoice** is fully paid: `min(round_half_up(10% * (subtotal - discount)), 50.00)`.
  Tax, late fees and the part paid by credit are not part of the base. One credit per
  referee, issued on the payment date, valid for 365 days.
- Decision: "first invoice" is literal. If the first invoice is still open and the
  referee pays a later one, no credit is earned (user story: "first invoice not fully
  paid, no credit exists").
- `issue_invoice` applies credits after tax: usable means `remaining > 0` and
  `expires_on > issue date`, oldest expiry first, partly usable, never more than the
  amount due. The invoice stays `ISSUED` even at a zero balance, and the remainder
  carries over. Credits are never paid out as cash and are not applied retroactively
  to invoices that were already issued.
- Known limitation: an invoice completely covered by credit has a zero balance, and
  `record_payment` (unchanged) only accepts positive payments, so such an invoice can
  never be marked `PAID` and would not trigger the referrer's credit.

## AI assistance

- Tools used: Claude (Anthropic), in the claude.ai chat interface.
- What I used them for: Claude read the assessment, explained the business rules and
  wrote the implementation, the tests, the commit messages and these notes. Commits
  carry a `Co-authored-by: Claude` trailer. The hiring email lists effective use of AI
  tools as one of the evaluation criteria.
- What I verified and can explain without assistance: TODO (candidate: replace this
  line with what is true for you, for example which tickets you re-ran and can walk
  through on your own).
- I confirm I did not paste the entire repository or assignment into an AI tool:
  **no.** I uploaded the whole repository to Claude, which generated the code.
