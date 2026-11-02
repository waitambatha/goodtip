from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("tipping", "0018_one_match_per_feed_game")]

    operations = [
        migrations.AddConstraint(
            model_name="match",
            constraint=models.UniqueConstraint(
                fields=["round", "external_id"], condition=~Q(external_id=""),
                name="uniq_match_external_per_round",
            ),
        ),
    ]
