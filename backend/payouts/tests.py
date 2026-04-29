import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.models import BigIntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.test import APIClient

from .models import BankAccount, LedgerEntry, Merchant, Payout
from .tasks import process_payout


def seed_merchant(balance_paise=10_000):
    merchant = Merchant.objects.create(name="Test Merchant", email=f"{uuid.uuid4()}@example.com")
    bank = BankAccount.objects.create(
        merchant=merchant,
        account_holder_name="Test Merchant",
        bank_name="HDFC Bank",
        last4="1234",
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=balance_paise,
        entry_type=LedgerEntry.EntryType.CREDIT,
        kind=LedgerEntry.Kind.CUSTOMER_PAYMENT,
        description="Test credit",
    )
    return merchant, bank


@pytest.mark.django_db(transaction=True)
def test_concurrent_payouts_cannot_overdraw_balance():
    if connection.vendor != "postgresql":
        pytest.skip("This race-condition test needs PostgreSQL row-level locks.")

    merchant, bank = seed_merchant(balance_paise=10_000)
    barrier = threading.Barrier(2)
    responses = []

    def post_payout():
        client = APIClient()
        barrier.wait()
        response = client.post(
            "/api/v1/payouts",
            {"amount_paise": 6_000, "bank_account_id": str(bank.id)},
            format="json",
            HTTP_X_MERCHANT_ID=str(merchant.id),
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        responses.append(response.status_code)

    threads = [threading.Thread(target=post_payout), threading.Thread(target=post_payout)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(responses) == [201, 422]
    assert Payout.objects.filter(merchant=merchant).count() == 1
    totals = LedgerEntry.objects.filter(merchant=merchant).aggregate(
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
    assert totals["credits"] - totals["debits"] == 4_000


@pytest.mark.django_db
def test_idempotency_key_returns_same_response_without_duplicate_payout():
    merchant, bank = seed_merchant(balance_paise=10_000)
    key = str(uuid.uuid4())
    client = APIClient()
    payload = {"amount_paise": 6_000, "bank_account_id": str(bank.id)}
    headers = {
        "HTTP_X_MERCHANT_ID": str(merchant.id),
        "HTTP_IDEMPOTENCY_KEY": key,
    }

    first = client.post("/api/v1/payouts", payload, format="json", **headers)
    second = client.post("/api/v1/payouts", payload, format="json", **headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert Payout.objects.filter(merchant=merchant).count() == 1


@pytest.mark.django_db
def test_failed_payout_refunds_with_credit_entry():
    merchant, bank = seed_merchant(balance_paise=10_000)
    client = APIClient()

    response = client.post(
        "/api/v1/payouts",
        {"amount_paise": 6_000, "bank_account_id": str(bank.id)},
        format="json",
        HTTP_X_MERCHANT_ID=str(merchant.id),
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    payout = Payout.objects.get(id=response.json()["id"])

    payout.status = Payout.Status.PROCESSING
    payout.save(update_fields=["status", "updated_at"])
    payout.transition_to(Payout.Status.FAILED, failure_reason="test failure")

    assert Merchant.balance_expression(merchant.id) == 10_000
    assert LedgerEntry.objects.filter(
        payout=payout,
        entry_type=LedgerEntry.EntryType.CREDIT,
        kind=LedgerEntry.Kind.PAYOUT_REFUND,
        amount_paise=6_000,
    ).exists()


@pytest.mark.django_db
def test_hung_payout_uses_exponential_backoff_and_max_attempt_refund():
    merchant, bank = seed_merchant(balance_paise=10_000)
    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank,
        amount_paise=6_000,
        status=Payout.Status.PENDING,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        payout=payout,
        amount_paise=6_000,
        entry_type=LedgerEntry.EntryType.DEBIT,
        kind=LedgerEntry.Kind.PAYOUT_HOLD,
        description="Hold for payout",
    )

    with patch("payouts.tasks.random.random", return_value=0.95):
        process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PROCESSING
    assert payout.attempts == 1
    assert payout.next_retry_at is not None

    payout.next_retry_at = payout.processing_started_at - timedelta(seconds=1)
    payout.save(update_fields=["next_retry_at"])
    with patch("payouts.tasks.random.random", return_value=0.95):
        process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PROCESSING
    assert payout.attempts == 2
    assert payout.next_retry_at is not None

    payout.next_retry_at = payout.processing_started_at - timedelta(seconds=1)
    payout.save(update_fields=["next_retry_at"])
    with patch("payouts.tasks.random.random", return_value=0.95):
        process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PROCESSING
    assert payout.attempts == 3

    payout.next_retry_at = payout.processing_started_at - timedelta(seconds=1)
    payout.save(update_fields=["next_retry_at"])
    process_payout(str(payout.id))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert Merchant.balance_expression(merchant.id) == 10_000
