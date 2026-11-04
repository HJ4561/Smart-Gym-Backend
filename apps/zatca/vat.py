"""
VAT engine: what the member paid already includes VAT, so each line is split into net + VAT,
exact to the halala, and the invoice total always equals what was paid.

  - a line's net is its paid amount / (1 + rate), rounded to the halala;
  - the net must equal quantity × unit price exactly, so a line that doesn't divide evenly is sent
    as two lines (3 × 10.00 → 2 × 8.70 + 1 × 8.69);
  - each line's VAT is its net × rate, rounded on its own (so one line may show ±0.01 against its
    price — "option A"); the document VAT is the total net × rate, and any halala left between that
    and what was paid goes into PayableRoundingAmount.

All maths is done in whole halalas (int) to avoid float or Decimal rounding drift.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

HUNDRED = Decimal(100)


def to_halalas(amount):
    return int((Decimal(str(amount)) * HUNDRED).to_integral_value(ROUND_HALF_UP))


def to_sar(halalas):
    return (Decimal(halalas) / HUNDRED).quantize(Decimal('0.01'))


def _round_div(numerator, denominator):
    """round_half_up(numerator / denominator) for non-negative ints."""
    return (2 * numerator + denominator) // (2 * denominator)


def _vat_of(net, rate_bp):
    """VAT in halalas of `net` halalas at `rate_bp` basis points (1500 = 15%)."""
    return _round_div(net * rate_bp, 10_000)


@dataclass
class Line:
    name: str
    quantity: int
    unit_net: int          # halalas, excluding VAT
    net: int               # quantity × unit_net
    vat: int

    @property
    def gross(self):
        return self.net + self.vat


@dataclass
class Breakdown:
    lines: list = field(default_factory=list)
    rate_bp: int = 1500
    net: int = 0               # Σ line net  (= TaxExclusiveAmount, no document allowances)
    vat: int = 0               # document VAT = round(net × rate)
    rounding: int = 0          # paid − (net + vat)
    paid: int = 0              # PayableAmount

    @property
    def inclusive(self):
        return self.net + self.vat

    @property
    def rate_percent(self):
        return (Decimal(self.rate_bp) / HUNDRED).quantize(Decimal('0.01'))


def split(items, rate_percent=Decimal('15')):
    """
    `items`: [(name, quantity, paid_amount_incl_vat), ...] — amounts in SAR (Decimal/str/number).
    Returns a Breakdown in halalas. Quantities are whole numbers ≥ 1; amounts ≥ 0.
    """
    rate_bp = int((Decimal(str(rate_percent)) * HUNDRED).to_integral_value(ROUND_HALF_UP))
    out = Breakdown(rate_bp=rate_bp)
    for name, quantity, paid in items:
        quantity = max(1, int(quantity))
        gross = to_halalas(paid)
        if gross < 0:
            raise ValueError('Line amounts must not be negative.')
        net = _round_div(gross * 10_000, 10_000 + rate_bp)
        unit, extra = divmod(net, quantity)
        # `extra` units cost one halala more, so quantity × unit price is exact on every line.
        parts = [(extra, unit + 1), (quantity - extra, unit)] if extra else [(quantity, unit)]
        for qty, unit_net in parts:
            if qty:
                line_net = qty * unit_net
                out.lines.append(Line(name, qty, unit_net, line_net, _vat_of(line_net, rate_bp)))
        out.paid += gross
    out.net = sum(l.net for l in out.lines)
    out.vat = _vat_of(out.net, rate_bp)
    out.rounding = out.paid - out.inclusive
    return out
