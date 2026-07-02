from django.db import migrations


def backfill_charity(apps, schema_editor):
    """Freeze the destination charity onto any pre-existing donation payments.

    New payments record their charity at creation; this fills in legacy rows from
    the charity their pledge points at.
    """
    DonationPayment = apps.get_model("billing", "DonationPayment")
    for payment in DonationPayment.objects.filter(charity__isnull=True).select_related("pledge").iterator():
        charity_id = payment.pledge.charity_id
        if charity_id:
            payment.charity_id = charity_id
            payment.save(update_fields=["charity"])


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_donationpayment_charity"),
    ]

    operations = [
        migrations.RunPython(backfill_charity, migrations.RunPython.noop),
    ]
