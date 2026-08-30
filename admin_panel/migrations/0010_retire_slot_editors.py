"""Drop the two page editors that PageEdit replaces.

`PageText` and `PageMedia` backed the slot editor (four public pages, each
editable sentence wrapped in a template tag); `SiteContent` backed the "Site
content" screen (the home page, declared slot by slot). The client asked for
one editor covering every public and private page instead, so both are gone
and their tables go with them.

Safe to drop rather than migrate across: all three tables were empty on both
instances when this was written — the editors shipped but nothing had been
edited through them yet — so there is no client copy to carry over. The
template defaults those editors overrode are still in the templates, untouched,
which is what the pages have been serving all along.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('admin_panel', '0009_pageedit'),
    ]

    operations = [
        migrations.DeleteModel(name='PageText'),
        migrations.DeleteModel(name='PageMedia'),
        migrations.DeleteModel(name='SiteContent'),
    ]
