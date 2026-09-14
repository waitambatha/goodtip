from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_user_onboarding_pages_seen"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="recap_codes",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
