# EXPLAINER

## The Ledger

Money lives in `ledger_entries`. Amounts are stored as integer paise using `BigIntegerField`; the UI converts paise to rupees only for display.

Every ledger row has a direction:

- `credit` increases available balance
- `debit` decreases available balance

Balance is never stored directly. It is computed from database aggregation:

```python
LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
    credits=Coalesce(
        Sum("amount_paise", filter=Q(entry_type=LedgerEntry.EntryType.CREDIT)),
        Value(0),
        output_field=BigIntegerField(),
    ),
    debits=Coalesce(
        Sum("amount_paise", filter=Q(entry_type=LedgerEntry.EntryType.DEBIT)),
        Value(0),
        output_field=BigIntegerField(),
    ),
)

balance = credits - debits
```

Customer payments are simulated by creating `credit` rows. Payout requests create `debit` hold rows immediately. Failed payouts create refund `credit` rows atomically with the failed state transition.

## The Lock

Overdraft prevention is handled in `payouts/services.py`.

```python
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    available_balance = Merchant.balance_expression(merchant.id)

    if available_balance < amount_paise:
        return {"detail": "Insufficient available balance."}, 422

    payout = Payout.objects.create(...)
    LedgerEntry.objects.create(
        merchant=merchant,
        payout=payout,
        amount_paise=amount_paise,
        entry_type=LedgerEntry.EntryType.DEBIT,
        kind=LedgerEntry.Kind.PAYOUT_HOLD,
    )
```

`select_for_update()` locks the merchant row. Concurrent payout requests for the same merchant serialize on that lock. The second request recomputes balance after the first debit hold exists, so it cannot overdraw.

## The Idempotency

Each payout request requires an `Idempotency-Key`. The system stores one `IdempotencyKey` row per `(merchant, key)` with a request hash, response body, response status, and 24-hour expiry.

```python
models.UniqueConstraint(
    fields=["merchant", "key"],
    name="uniq_idempotency_key_per_merchant",
)
```

The request handler locks the idempotency row with `select_for_update()`. Duplicate submissions with the same key return the original response. Reusing the same key with a different request body returns `409`.

## The State Machine

Legal payout transitions are defined on the `Payout` model:

```python
LEGAL_TRANSITIONS = {
    Status.PENDING: {Status.PROCESSING},
    Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
    Status.COMPLETED: set(),
    Status.FAILED: set(),
}
```

The model rejects illegal backwards transitions:

```python
if new_status not in self.LEGAL_TRANSITIONS[self.status]:
    raise ValidationError(...)
```

Failed payout refunds happen inside the transition path and are called from `transaction.atomic()`, so the status change and refund credit commit together.

## The Worker Simulation

Celery processes payouts asynchronously:

- `70%` complete
- `20%` fail and refund
- `10%` remain processing to simulate delay

```python
outcome = random.random()

if outcome < 0.70:
    payout.transition_to(Payout.Status.COMPLETED)
elif outcome < 0.90:
    payout.transition_to(Payout.Status.FAILED, failure_reason="Bank rejected the payout.")
else:
    pass
```

Processing payouts stuck longer than 30 seconds are retried. After 3 attempts, the payout is failed and refunded atomically.

## The AI Audit

A subtly wrong version of the payout creation code used Python arithmetic over fetched ledger rows:

```python
entries = LedgerEntry.objects.filter(merchant=merchant)
available_balance = sum(
    entry.amount_paise if entry.entry_type == "credit" else -entry.amount_paise
    for entry in entries
)

if available_balance >= amount_paise:
    LedgerEntry.objects.create(
        amount_paise=amount_paise,
        entry_type="debit",
        ...
    )
```

That is unsafe for two reasons:

- it calculates balance in Python instead of the database
- two concurrent requests can both read the same old balance before either inserts its debit

I replaced it with a database aggregate and a row-level lock:

```python
with transaction.atomic():
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    available_balance = Merchant.balance_expression(merchant.id)
```

The important part is the shared database lock. A transaction alone is not enough; every competing payout for the same merchant must be forced through the same locked row before reading balance and inserting the debit hold.
