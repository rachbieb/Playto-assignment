from django.core.management.base import BaseCommand
from django.db import transaction

from payouts.models import BankAccount, LedgerEntry, Merchant


class Command(BaseCommand):
    help = "Seed demo merchants, bank accounts, and credit ledger history."

    def handle(self, *args, **options):
        merchants = [
            ("Saffron Digital Agency", "hello@saffron.co", 442_550_00),
            ("PixelForge Agency", "finance@pixelforge.example", 192_500_00),
            ("Indie SaaS Lab", "founder@indiesaas.example", 241_000_00),
        ]

        with transaction.atomic():
            for name, email, total_credit in merchants:
                merchant, _ = Merchant.objects.get_or_create(
                    email=email,
                    defaults={"name": name},
                )
                BankAccount.objects.get_or_create(
                    merchant=merchant,
                    last4="4821",
                    defaults={
                        "account_holder_name": name,
                        "bank_name": "HDFC Bank",
                    },
                )
                if not merchant.ledger_entries.filter(kind=LedgerEntry.Kind.CUSTOMER_PAYMENT).exists():
                    chunks = [total_credit // 2, total_credit // 3, total_credit - (total_credit // 2) - (total_credit // 3)]
                    for idx, amount in enumerate(chunks, start=1):
                        LedgerEntry.objects.create(
                            merchant=merchant,
                            amount_paise=amount,
                            entry_type=LedgerEntry.EntryType.CREDIT,
                            kind=LedgerEntry.Kind.CUSTOMER_PAYMENT,
                            description=f"Simulated USD customer payment #{idx}",
                        )

        self.stdout.write(self.style.SUCCESS("Seeded Playto demo data."))
