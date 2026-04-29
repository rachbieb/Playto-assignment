import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import BigIntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @classmethod
    def balance_expression(cls, merchant_id):
        totals = LedgerEntry.objects.filter(merchant_id=merchant_id).aggregate(
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
        return totals["credits"] - totals["debits"]

    @property
    def available_balance_paise(self):
        return self.balance_expression(self.id)

    @property
    def held_balance_paise(self):
        return (
            Payout.objects.filter(
                merchant_id=self.id,
                status__in=[Payout.Status.PENDING, Payout.Status.PROCESSING],
            ).aggregate(total=Coalesce(Sum("amount_paise"), Value(0), output_field=BigIntegerField()))["total"]
        )

    @property
    def lifetime_credits_paise(self):
        return LedgerEntry.objects.filter(
            merchant_id=self.id,
            entry_type=LedgerEntry.EntryType.CREDIT,
            kind=LedgerEntry.Kind.CUSTOMER_PAYMENT,
        ).aggregate(total=Coalesce(Sum("amount_paise"), Value(0), output_field=BigIntegerField()))["total"]


class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, related_name="bank_accounts", on_delete=models.CASCADE)
    account_holder_name = models.CharField(max_length=160)
    bank_name = models.CharField(max_length=120)
    last4 = models.CharField(max_length=4)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.bank_name} ****{self.last4}"


class LedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    class Kind(models.TextChoices):
        CUSTOMER_PAYMENT = "customer_payment", "Customer payment"
        PAYOUT_HOLD = "payout_hold", "Payout hold"
        PAYOUT_REFUND = "payout_refund", "Failed payout refund"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, related_name="ledger_entries", on_delete=models.CASCADE)
    amount_paise = models.BigIntegerField()
    entry_type = models.CharField(max_length=8, choices=EntryType.choices)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    description = models.CharField(max_length=240)
    payout = models.ForeignKey(
        "Payout",
        related_name="ledger_entries",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["merchant", "-created_at"]),
            models.Index(fields=["merchant", "entry_type"], name="payouts_led_merchan_44fa06_idx"),
            models.Index(fields=["payout", "kind"]),
        ]

    def clean(self):
        if self.amount_paise <= 0:
            raise ValidationError("Ledger amounts must be positive paise values.")
        if self.kind == self.Kind.PAYOUT_HOLD and self.entry_type != self.EntryType.DEBIT:
            raise ValidationError("Payout holds must be debit entries.")
        if self.kind == self.Kind.PAYOUT_REFUND and self.entry_type != self.EntryType.CREDIT:
            raise ValidationError("Payout refunds must be credit entries.")
        if self.kind == self.Kind.CUSTOMER_PAYMENT and self.entry_type != self.EntryType.CREDIT:
            raise ValidationError("Customer payments must be credit entries.")


class Payout(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    LEGAL_TRANSITIONS = {
        Status.PENDING: {Status.PROCESSING},
        Status.PROCESSING: {Status.COMPLETED, Status.FAILED},
        Status.COMPLETED: set(),
        Status.FAILED: set(),
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, related_name="payouts", on_delete=models.CASCADE)
    bank_account = models.ForeignKey(BankAccount, related_name="payouts", on_delete=models.PROTECT)
    amount_paise = models.BigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_retry_at"]),
            models.Index(fields=["merchant", "-created_at"]),
        ]

    def transition_to(self, new_status, *, failure_reason=""):
        if new_status not in self.LEGAL_TRANSITIONS[self.status]:
            raise ValidationError(f"Illegal payout transition {self.status} -> {new_status}")

        old_status = self.status
        self.status = new_status
        if new_status == self.Status.PROCESSING:
            self.processing_started_at = timezone.now()
        if new_status == self.Status.FAILED:
            self.failure_reason = failure_reason

        update_fields = ["status", "processing_started_at", "failure_reason", "updated_at"]
        self.save(update_fields=update_fields)

        if old_status == self.Status.PROCESSING and new_status == self.Status.FAILED:
            LedgerEntry.objects.create(
                merchant=self.merchant,
                payout=self,
                amount_paise=self.amount_paise,
                entry_type=LedgerEntry.EntryType.CREDIT,
                kind=LedgerEntry.Kind.PAYOUT_REFUND,
                description=f"Refund for failed payout {self.id}",
            )

    def mark_retry_started(self):
        self.processing_started_at = timezone.now()
        self.next_retry_at = None
        self.save(update_fields=["processing_started_at", "next_retry_at", "updated_at"])

    def schedule_retry(self):
        delay_seconds = 30 * (2 ** max(self.attempts - 1, 0))
        self.next_retry_at = timezone.now() + timedelta(seconds=delay_seconds)
        self.save(update_fields=["next_retry_at", "updated_at"])


class IdempotencyKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, related_name="idempotency_keys", on_delete=models.CASCADE)
    key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    response_body = models.JSONField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["merchant", "key"], name="uniq_idempotency_key_per_merchant")
        ]

    @property
    def is_complete(self):
        return self.response_body is not None and self.response_status is not None
