from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_waitlist_accounts_and_campaign'),
    ]

    operations = [
        migrations.AddField(
            model_name='waitlistcampaign',
            name='launch_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
