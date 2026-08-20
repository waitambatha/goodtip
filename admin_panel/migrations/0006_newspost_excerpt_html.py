from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_panel", "0005_alter_newspost_body"),
    ]

    operations = [
        migrations.AddField(
            model_name="newspost",
            name="excerpt_html",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="newspost",
            name="link_url",
            field=models.URLField(blank=True, help_text="Deprecated, use `sources`."),
        ),
    ]
