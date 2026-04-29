from django.db import migrations, models


def migrate_ledger_entries(apps, schema_editor):
    LedgerEntry = apps.get_model("payouts", "LedgerEntry")

    for entry in LedgerEntry.objects.all().iterator():
        if entry.kind == "credit":
            entry.kind = "customer_payment"
            entry.entry_type = "credit"
        elif entry.kind == "payout_release":
            entry.kind = "payout_refund"
            entry.entry_type = "credit"
        elif entry.kind == "payout_hold":
            entry.entry_type = "debit"
        else:
            entry.entry_type = "credit" if entry.amount_paise >= 0 else "debit"

        entry.amount_paise = abs(entry.amount_paise)
        entry.save(update_fields=["kind", "entry_type", "amount_paise"])


def rollback_ledger_entries(apps, schema_editor):
    LedgerEntry = apps.get_model("payouts", "LedgerEntry")

    for entry in LedgerEntry.objects.all().iterator():
        if entry.kind == "customer_payment":
            entry.kind = "credit"
        elif entry.kind == "payout_refund":
            entry.kind = "payout_release"

        if entry.entry_type == "debit":
            entry.amount_paise = -abs(entry.amount_paise)
        else:
            entry.amount_paise = abs(entry.amount_paise)
        entry.save(update_fields=["kind", "amount_paise"])


class Migration(migrations.Migration):
    dependencies = [
        ("payouts", "0002_rename_payouts_led_merchan_871057_idx_payouts_led_merchan_31a74c_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="ledgerentry",
            name="entry_type",
            field=models.CharField(
                choices=[("credit", "Credit"), ("debit", "Debit")],
                default="credit",
                max_length=8,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(migrate_ledger_entries, rollback_ledger_entries),
        migrations.AlterField(
            model_name="ledgerentry",
            name="kind",
            field=models.CharField(
                choices=[
                    ("customer_payment", "Customer payment"),
                    ("payout_hold", "Payout hold"),
                    ("payout_refund", "Failed payout refund"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(fields=["merchant", "entry_type"], name="payouts_led_merchan_44fa06_idx"),
        ),
    ]
