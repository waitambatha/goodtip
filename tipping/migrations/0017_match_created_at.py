# Match.created_at — when a fixture first reached GoodTip (27 Oct 2026).
#
# Rows from before this migration are left NULL on purpose: Django would
# otherwise stamp every existing fixture with the moment the migration ran,
# and every game already played would then read as "arrived after kick-off"
# — switching off the missed-tip default for re-grades and late joiners
# across the whole history. NULL keeps the old behaviour for them.
from django.db import migrations, models


def clear(apps, schema_editor):
    Match = apps.get_model("tipping", "Match")
    Match.objects.using(schema_editor.connection.alias).update(created_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('tipping', '0016_team_logo_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True, blank=True),
        ),
        migrations.RunPython(clear, migrations.RunPython.noop),
    ]
