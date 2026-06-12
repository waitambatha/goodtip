from django.db import migrations


def forwards(apps, schema_editor):
    OrgMember = apps.get_model("orgs", "OrgMember")
    # Old 'admin' members ran the league and are the de-facto owner.
    OrgMember.objects.filter(role="admin").update(role="both", is_league_owner=True)
    # Everyone else was a participant.
    OrgMember.objects.filter(role="member").update(role="participant")


def backwards(apps, schema_editor):
    OrgMember = apps.get_model("orgs", "OrgMember")
    OrgMember.objects.filter(role__in=["both", "manager", "captain"]).update(role="admin")
    OrgMember.objects.filter(role="participant").update(role="member")


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0009_orgmember_is_league_owner_alter_orgmember_role"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
