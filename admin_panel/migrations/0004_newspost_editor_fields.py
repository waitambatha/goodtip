from django.db import migrations, models
from django.utils.text import slugify


def populate_slugs(apps, schema_editor):
    NewsPost = apps.get_model('admin_panel', 'NewsPost')
    seen = set(NewsPost.objects.exclude(slug='').values_list('slug', flat=True))
    for post in NewsPost.objects.filter(slug=''):
        base = slugify(post.title)[:200] or 'post'
        slug = base
        suffix = 2
        while slug in seen:
            slug = f"{base}-{suffix}"
            suffix += 1
        seen.add(slug)
        post.slug = slug
        post.save(update_fields=['slug'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('admin_panel', '0003_enquiry'),
    ]

    operations = [
        migrations.AddField(
            model_name='newspost',
            name='title_html',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='newspost',
            name='sources',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='newspost',
            name='slug',
            # db_index=False here: SlugField defaults to db_index=True, which
            # would build a plain index (plus Postgres's varchar_pattern_ops
            # "_like" index) now and another when AlterField below adds the
            # unique index — same auto-generated name, so the second create
            # collides with the first ("relation ... already exists"). There's
            # no query running against this column before the unique index
            # exists, so it doesn't need one in the meantime.
            field=models.SlugField(blank=True, db_index=False, default='', max_length=220),
            preserve_default=False,
        ),
        migrations.RunPython(populate_slugs, noop),
        migrations.AlterField(
            model_name='newspost',
            name='slug',
            field=models.SlugField(blank=True, max_length=220, unique=True),
        ),
    ]
