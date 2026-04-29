import uuid

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import LedgerEntry, Merchant, Payout
from .serializers import (
    CreditCreateSerializer,
    DashboardSerializer,
    LedgerEntrySerializer,
    PayoutCreateSerializer,
    PayoutSerializer,
)
from .services import create_payout_idempotently
from .tasks import process_due_payouts


def get_merchant(request):
    merchant_id = request.headers.get("X-Merchant-Id")
    if merchant_id:
        return get_object_or_404(Merchant, id=merchant_id)
    return Merchant.objects.order_by("created_at").first()


@api_view(["GET"])
def merchants(request):
    data = [
        {"id": str(merchant.id), "name": merchant.name, "email": merchant.email}
        for merchant in Merchant.objects.order_by("created_at")
    ]
    return Response(data)


@api_view(["GET"])
def dashboard(request):
    merchant = get_merchant(request)
    if merchant is None:
        return Response({"detail": "No merchants seeded."}, status=status.HTTP_404_NOT_FOUND)

    payload = {
        "merchant": merchant,
        "bank_accounts": merchant.bank_accounts.order_by("created_at"),
        "recent_ledger": LedgerEntry.objects.filter(merchant=merchant).order_by("-created_at")[:20],
        "payouts": Payout.objects.filter(merchant=merchant).select_related("bank_account").order_by("-created_at")[:50],
    }
    return Response(DashboardSerializer(payload).data)


@api_view(["GET", "POST"])
def payouts(request):
    merchant = get_merchant(request)
    if merchant is None:
        return Response({"detail": "No merchants seeded."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        qs = Payout.objects.filter(merchant=merchant).select_related("bank_account").order_by("-created_at")
        return Response(PayoutSerializer(qs, many=True).data)

    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return Response({"detail": "Idempotency-Key header is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        parsed_key = uuid.UUID(idempotency_key)
    except ValueError:
        return Response({"detail": "Idempotency-Key must be a UUID."}, status=status.HTTP_400_BAD_REQUEST)

    serializer = PayoutCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    body, response_status = create_payout_idempotently(
        merchant_id=merchant.id,
        key=parsed_key,
        payload=serializer.validated_data,
    )
    return Response(body, status=response_status)


@api_view(["POST"])
def simulate_credit(request):
    merchant = get_merchant(request)
    if merchant is None:
        return Response({"detail": "No merchants seeded."}, status=status.HTTP_404_NOT_FOUND)

    serializer = CreditCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    entry = LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=serializer.validated_data["amount_paise"],
        entry_type=LedgerEntry.EntryType.CREDIT,
        kind=LedgerEntry.Kind.CUSTOMER_PAYMENT,
        description=serializer.validated_data["description"],
    )
    return Response(LedgerEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def process_batch(request):
    try:
        count = int(request.data.get("count", 1) or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 5))
    task_ids = [process_due_payouts.delay().id for _ in range(count)]
    return Response({"queued": count, "task_ids": task_ids}, status=status.HTTP_202_ACCEPTED)
