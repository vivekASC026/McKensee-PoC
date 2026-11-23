"""What a claim paid: the allowed amount, the patient's share, the payment.

The pattern store holds what each claim line charged and the claim's status,
never what it paid -- its allowed, paid, deductible, copay and coinsurance
columns are empty. So a remittance cannot copy a payment; it works one out,
the way a payer does, from terms the 835's description states
(`"adjudication"` in edi_formats/835.json):

    denied_statuses    claim statuses that pay nothing
    denial_reason      the CARC a denied line is adjusted with (CO)
    allowed_share      the allowed amount, a share of the charge, low to high
    deductible_share   the deductible, a share of the allowed amount
    copays             a copay, one of these, on a claim's first line
    coinsurance_share  coinsurance, a share of what is left

Whatever the charge is above the allowed amount is the contract's (CO 45);
deductible, coinsurance and copay are the patient's (PR 1, 2, 3); the rest is
paid. Every line balances: its charge less its adjustments is its payment, as
X221A1 requires.

Each value is drawn from the file's seed, the claim and the line, so the same
claim pays the same in every file written of it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")

TERMS = {
    "denied_statuses": ["04"],
    "denial_reason": "96",
    "allowed_share": [0.5, 0.95],
    "deductible_share": [0.0, 0.15],
    "copays": [0, 15, 20, 25, 30, 40, 50],
    "coinsurance_share": [0.0, 0.2],
}


def _v1(lines: list[dict[str, Any]], terms: dict[str, Any], seed: str) -> list[dict[str, Any]]:
    """Version 1: every line is paid what it charged, as the 835 always wrote."""
    return [{**line, "PAID_AMOUNT": line.get("CHARGE_AMOUNT") or "0"} for line in lines]


def _v2(lines: list[dict[str, Any]], terms: dict[str, Any], seed: str) -> list[dict[str, Any]]:
    """Version 2: a denied claim pays nothing; a paid one pays the allowed
    amount less the patient's share."""
    from sdg.writers.elements import _draw

    terms = {**TERMS, **(terms or {})}
    denied = {str(s).strip() for s in terms["denied_statuses"]}
    status = str(lines[0].get("CLAIM_STATUS") or "").strip() if lines else ""
    claim = str(lines[0].get("CLAIM_ID") or "") if lines else ""
    out = []
    for number, line in enumerate(lines, start=1):
        charge = _money(line.get("CHARGE_AMOUNT"))
        line = {**line, "CHARGE_AMOUNT": f"{charge:.2f}"}
        if status in denied:
            line.update(CLAIM_PAYMENT_STATUS="4", PAID_AMOUNT="0.00")
            if charge:
                line.update(CO_REASON=str(terms["denial_reason"]), CO_AMOUNT=f"{charge:.2f}")
            out.append(line)
            continue

        def share(name: str, of: Decimal) -> Decimal:
            low, high = (float(v) for v in terms[name])
            drawn = low + (high - low) * _draw(seed, name, claim, number)
            return (of * Decimal(str(drawn))).quantize(CENT, ROUND_HALF_UP)

        allowed = min(charge, share("allowed_share", charge))
        deductible = share("deductible_share", allowed)
        copay = Decimal("0")
        if number == 1 and terms["copays"]:
            listed = terms["copays"]
            picked = listed[int(_draw(seed, "copay", claim) * len(listed)) % len(listed)]
            copay = min(Decimal(str(picked)).quantize(CENT), allowed - deductible)
        coinsurance = share("coinsurance_share", allowed - deductible - copay)
        paid = allowed - deductible - copay - coinsurance
        line.update(
            CLAIM_PAYMENT_STATUS="1",
            ALLOWED_AMOUNT=f"{allowed:.2f}",
            PAID_AMOUNT=f"{paid:.2f}",
            PATIENT_RESPONSIBILITY=f"{deductible + copay + coinsurance:.2f}",
        )
        if charge > allowed:
            line.update(CO_REASON="45", CO_AMOUNT=f"{charge - allowed:.2f}")
        owed = [(code, amount) for code, amount in
                (("1", deductible), ("2", coinsurance), ("3", copay)) if amount > 0]
        for place, (code, amount) in enumerate(owed, start=1):
            line[f"PR_REASON_{place}"] = code
            line[f"PR_AMOUNT_{place}"] = f"{amount:.2f}"
        out.append(line)
    return out


def _money(value: Any) -> Decimal:
    try:
        return max(Decimal("0"), Decimal(str(value or "0").strip() or "0")).quantize(
            CENT, ROUND_HALF_UP)
    except InvalidOperation:
        return Decimal("0")


def adjudicated(fmt, records, rules: dict[str, dict], *, seed: str, as_of: str):
    """Records with what each claim paid, a claim at a time.

    A record's charge and status are what the file writes for it: held, or
    drawn by their rules at the record's place -- the same draw the 835's own
    segments would make, so adding the payment changes no other value.
    """
    from sdg.engine.generators import algorithm
    from sdg.writers.edi_formats.registry import _grouped
    from sdg.writers.edi_formats.resolve import _value_for

    place = 0
    for group in _grouped(fmt, records):
        start = place + 1
        lines = []
        for record in group:
            place += 1
            charge = _value_for("CHARGE_AMOUNT", record, rules, seed=seed,
                                row_index=place, as_of=as_of)
            lines.append({**record, "CHARGE_AMOUNT": charge})
        if lines[0].get("CLAIM_STATUS") in (None, "") and "CLAIM_STATUS" in rules:
            status = _value_for("CLAIM_STATUS", lines[0], rules, seed=seed,
                                row_index=start, as_of=as_of)
            lines = [{**line, "CLAIM_STATUS": status} for line in lines]
        yield from algorithm("adjudication")(lines, fmt.adjudication, seed)


def _register() -> None:
    from sdg.engine.generators import register_algorithm

    register_algorithm("adjudication", 1, _v1)
    register_algorithm("adjudication", 2, _v2)


_register()
