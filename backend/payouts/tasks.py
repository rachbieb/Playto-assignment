import random
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Payout


MAX_ATTEMPTS = 3
STUCK_PROCESSING_AFTER = timedelta(seconds=30)


@shared_task
def process_due_payouts():
    now = timezone.now()
    pending_ids = list(
        Payout.objects.filter(status=Payout.Status.PENDING)
        .filter(next_retry_at__isnull=True)
        .values_list("id", flat=True)[:25]
    )
    retry_ids = list(
        Payout.objects.filter(
            status=Payout.Status.PROCESSING,
        )
        .filter(
            Q(next_retry_at__lte=now)
            | Q(next_retry_at__isnull=True, processing_started_at__lt=now - STUCK_PROCESSING_AFTER)
        )
        .values_list("id", flat=True)[:25]
    )
    for payout_id in pending_ids + retry_ids:
        process_payout.delay(str(payout_id))


@shared_task(bind=True)
def process_payout(self, payout_id):
    with transaction.atomic():
        payout = Payout.objects.select_for_update().select_related("merchant").get(id=payout_id)
        if payout.status == Payout.Status.PENDING:
            payout.transition_to(Payout.Status.PROCESSING)
        elif payout.status == Payout.Status.PROCESSING:
            if payout.next_retry_at and payout.next_retry_at > timezone.now():
                return
            if not payout.next_retry_at and payout.processing_started_at > timezone.now() - STUCK_PROCESSING_AFTER:
                return
            if payout.attempts >= MAX_ATTEMPTS:
                payout.transition_to(Payout.Status.FAILED, failure_reason="Bank settlement timed out.")
                return
            payout.mark_retry_started()
        else:
            return

        payout.attempts += 1
        payout.next_retry_at = None
        payout.save(update_fields=["attempts", "next_retry_at", "updated_at"])

    outcome = random.random()

    with transaction.atomic():
        payout = Payout.objects.select_for_update().select_related("merchant").get(id=payout_id)
        if payout.status != Payout.Status.PROCESSING:
            return

        if outcome < 0.70:
            payout.transition_to(Payout.Status.COMPLETED)
        elif outcome < 0.90:
            payout.transition_to(Payout.Status.FAILED, failure_reason="Bank rejected the payout.")
        else:
            payout.schedule_retry()
