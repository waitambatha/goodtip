"""SEO fields, multi-code tags, scheduling, and the redirect table.

The one thing here that is not additive is `tags`, and it is still additive at
the database level: the old `tag` column is left exactly where it is and kept
in step by `NewsPost.save`. See the field comments on the model for why a
column with data in it is not dropped in the same change that stops relying
on it.
"""
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def backfill_tags(apps, schema_editor):
    """Give every existing story a one-item `tags` list holding its old tag.

    Without this, every post written before today reads as untagged the moment
    the templates start asking `tags` — which would empty the news list's code
    filters on the day this deploys. `tag_list` falls back to `tag` anyway, so
    this is belt and braces; it is here so the DATA is right, not just the
    reading of it, and so a future query can filter on `tags` alone.
    """
    NewsPost = apps.get_model("admin_panel", "NewsPost")
    for post in NewsPost.objects.exclude(tag="").only("id", "tag", "tags").iterator():
        if not post.tags:
            post.tags = [post.tag]
            post.save(update_fields=["tags"])


def unbackfill_tags(apps, schema_editor):
    """Nothing to undo — `tag` was never touched, and `tags` goes with the
    column when this migration is reversed."""


class Migration(migrations.Migration):

    dependencies = [
        ('admin_panel', '0010_retire_slot_editors'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='newspost',
            name='canonical_url',
            field=models.URLField(blank=True, help_text='Only when this page duplicates another one. Points search engines at the address that should be indexed instead.'),
        ),
        migrations.AddField(
            model_name='newspost',
            name='image_alt',
            field=models.CharField(blank=True, help_text='What the picture shows, for screen readers and search engines.', max_length=200),
        ),
        migrations.AddField(
            model_name='newspost',
            name='meta_description',
            field=models.CharField(blank=True, help_text='The grey summary under the link in Google. Leave blank to use the teaser. Around 155 characters before it is cut off.', max_length=320),
        ),
        migrations.AddField(
            model_name='newspost',
            name='meta_title',
            field=models.CharField(blank=True, help_text='The browser tab and the blue line in Google. Leave blank to use the headline. Around 60 characters before it is cut off.', max_length=200),
        ),
        migrations.AddField(
            model_name='newspost',
            name='og_description',
            field=models.CharField(blank=True, help_text='The text under it on the share card. Leave blank to use the meta description.', max_length=320),
        ),
        migrations.AddField(
            model_name='newspost',
            name='og_image',
            field=models.ImageField(blank=True, help_text="The picture on the share card. 1200x630 reads best. Leave blank to use the page's own image.", null=True, upload_to='seo/'),
        ),
        migrations.AddField(
            model_name='newspost',
            name='og_title',
            field=models.CharField(blank=True, help_text='The headline on a Facebook or LinkedIn share card. Leave blank to use the meta title.', max_length=200),
        ),
        migrations.AddField(
            model_name='newspost',
            name='robots_follow',
            field=models.BooleanField(default=True, help_text='Uncheck to stop links on this page passing on ranking.'),
        ),
        migrations.AddField(
            model_name='newspost',
            name='robots_index',
            field=models.BooleanField(default=True, help_text='Uncheck to keep this page out of Google.'),
        ),
        migrations.AddField(
            model_name='newspost',
            name='tags',
            field=models.JSONField(blank=True, default=list, help_text='Every code this story is about. A piece on AFLW and NRLW shows under both.'),
        ),
        migrations.AddField(
            model_name='pageedit',
            name='image_alt',
            field=models.CharField(blank=True, help_text='What the new picture shows, for screen readers and search engines.', max_length=200),
        ),
        migrations.AlterField(
            model_name='newspost',
            name='published_at',
            field=models.DateTimeField(default=django.utils.timezone.now, help_text='When this story counts as published. A date in the past backdates it; a date in the future holds it back until then.'),
        ),
        migrations.AlterField(
            model_name='newspost',
            name='slug',
            field=models.SlugField(blank=True, help_text="The last part of the story's address. Changing it leaves a redirect behind so links already shared keep working.", max_length=220, unique=True),
        ),
        migrations.CreateModel(
            name='PageSeo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('meta_title', models.CharField(blank=True, help_text='The browser tab and the blue line in Google. Leave blank to use the headline. Around 60 characters before it is cut off.', max_length=200)),
                ('meta_description', models.CharField(blank=True, help_text='The grey summary under the link in Google. Leave blank to use the teaser. Around 155 characters before it is cut off.', max_length=320)),
                ('og_title', models.CharField(blank=True, help_text='The headline on a Facebook or LinkedIn share card. Leave blank to use the meta title.', max_length=200)),
                ('og_description', models.CharField(blank=True, help_text='The text under it on the share card. Leave blank to use the meta description.', max_length=320)),
                ('og_image', models.ImageField(blank=True, help_text="The picture on the share card. 1200x630 reads best. Leave blank to use the page's own image.", null=True, upload_to='seo/')),
                ('canonical_url', models.URLField(blank=True, help_text='Only when this page duplicates another one. Points search engines at the address that should be indexed instead.')),
                ('robots_index', models.BooleanField(default=True, help_text='Uncheck to keep this page out of Google.')),
                ('robots_follow', models.BooleanField(default=True, help_text='Uncheck to stop links on this page passing on ranking.')),
                ('page', models.CharField(max_length=40, unique=True)),
                ('path_override', models.CharField(blank=True, help_text='Serve this page at a different address, e.g. /why-goodtip/. The old address redirects here, so nothing already shared breaks. Leave blank to keep the built-in one.', max_length=200)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='page_seo_edits', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'page SEO',
                'verbose_name_plural': 'page SEO',
                'ordering': ['page'],
            },
        ),
        migrations.CreateModel(
            name='Redirect',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('old_path', models.CharField(db_index=True, help_text='The address that no longer exists, e.g. /old-pricing/. Start it with a slash; leave the domain off.', max_length=200, unique=True)),
                ('new_path', models.CharField(help_text='Where it should go instead. A path like /pricing/, or a full https:// address for somewhere off the site.', max_length=400)),
                ('is_permanent', models.BooleanField(default=True, help_text='Permanent (301) moves the search ranking to the new address. Uncheck for a temporary (302) move.')),
                ('note', models.CharField(blank=True, help_text='Why this exists, for whoever finds it in a year.', max_length=200)),
                ('hits', models.PositiveIntegerField(default=0)),
                ('last_hit_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='redirects', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['old_path'],
            },
        ),
        migrations.RunPython(backfill_tags, unbackfill_tags),
    ]
