from rest_framework import serializers

from .models import BankAccount, LedgerEntry, Merchant, Payout


class MerchantSerializer(serializers.ModelSerializer):
    available_balance_paise = serializers.IntegerField(read_only=True)
    held_balance_paise = serializers.IntegerField(read_only=True)
    lifetime_credits_paise = serializers.IntegerField(read_only=True)

    class Meta:
        model = Merchant
        fields = ["id", "name", "email", "available_balance_paise", "held_balance_paise", "lifetime_credits_paise"]


class BankAccountSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = ["id", "account_holder_name", "bank_name", "last4", "label"]

    def get_label(self, obj):
        return f"{obj.bank_name} ****{obj.last4}"


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = ["id", "amount_paise", "entry_type", "kind", "description", "payout_id", "created_at"]


class PayoutSerializer(serializers.ModelSerializer):
    bank_account = BankAccountSerializer(read_only=True)

    class Meta:
        model = Payout
        fields = [
            "id",
            "amount_paise",
            "status",
            "attempts",
            "failure_reason",
            "bank_account",
            "created_at",
            "updated_at",
        ]


class PayoutCreateSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.UUIDField()


class CreditCreateSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1)
    description = serializers.CharField(max_length=240, required=False, default="Simulated customer payment")


class DashboardSerializer(serializers.Serializer):
    merchant = MerchantSerializer()
    bank_accounts = BankAccountSerializer(many=True)
    recent_ledger = LedgerEntrySerializer(many=True)
    payouts = PayoutSerializer(many=True)
