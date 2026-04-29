import hashlib
import json
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from .models import BankAccount, IdempotencyKey, LedgerEntry, Merchant, Payout
from .serializers import PayoutSerializer


def canonical_request_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_payout_idempotently(*, merchant_id, key, payload):
    request_hash = canonical_request_hash(payload)
    now = timezone.now()

    with transaction.atomic():
        IdempotencyKey.objects.filter(merchant_id=merchant_id, key=key, expires_at__lt=now).delete()
        idem = _get_locked_idempotency_key(
            merchant_id=merchant_id,
            key=key,
            request_hash=request_hash,
            expires_at=now + timedelta(hours=24),
        )

        if idem.request_hash != request_hash:
            return {"detail": "Idempotency-Key was already used with a different request body."}, 409

        if idem.is_complete:
            return idem.response_body, idem.response_status

        response_body, response_status = _create_payout_under_lock(merchant_id=merchant_id, payload=payload)
        idem.response_body = response_body
        idem.response_status = response_status
        idem.save(update_fields=["response_body", "response_status"])
        return response_body, response_status


def _get_locked_idempotency_key(*, merchant_id, key, request_hash, expires_at):
    try:
        idem, _created = IdempotencyKey.objects.select_for_update().get_or_create(
            merchant_id=merchant_id,
            key=key,
            defaults={"request_hash": request_hash, "expires_at": expires_at},
        )
        return idem
    except IntegrityError:
        return IdempotencyKey.objects.select_for_update().get(merchant_id=merchant_id, key=key)


def _create_payout_under_lock(*, merchant_id, payload):
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)

    try:
        bank_account = BankAccount.objects.get(id=payload["bank_account_id"], merchant=merchant)
    except BankAccount.DoesNotExist:
        return {"detail": "Bank account not found for merchant."}, status.HTTP_400_BAD_REQUEST

    amount_paise = int(payload["amount_paise"])
    available_balance = Merchant.balance_expression(merchant.id)
    if available_balance < amount_paise:
        return {
            "detail": "Insufficient available balance.",
            "available_balance_paise": available_balance,
        }, status.HTTP_422_UNPROCESSABLE_ENTITY

    payout = Payout.objects.create(
        merchant=merchant,
        bank_account=bank_account,
        amount_paise=amount_paise,
        status=Payout.Status.PENDING,
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        payout=payout,
        amount_paise=amount_paise,
        entry_type=LedgerEntry.EntryType.DEBIT,
        kind=LedgerEntry.Kind.PAYOUT_HOLD,
        description=f"Hold for payout {payout.id}",
    )
    return PayoutSerializer(payout).data, status.HTTP_201_CREATED


def transition_payout(payout_id, new_status, *, failure_reason=""):
    with transaction.atomic():
        payout = Payout.objects.select_for_update().select_related("merchant").get(id=payout_id)
        payout.transition_to(new_status, failure_reason=failure_reason)
        return payout


def fail_processing_payout(payout_id, reason):
    return transition_payout(payout_id, Payout.Status.FAILED, failure_reason=reason)


def validate_transition(current_status, new_status):
    if new_status not in Payout.LEGAL_TRANSITIONS[current_status]:
        raise ValidationError(f"Illegal payout transition {current_status} -> {new_status}")
