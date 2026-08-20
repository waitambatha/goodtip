"""Follow catalog's rename, and drop the department columns Group replaced.

`Organisation.group_type` meant the organisation's type, so it is renamed to
say that. The two department columns go: a department is a group now, and a
group is the thing that has a kind, so `department_type` and
`department_label` have nowhere to point and nothing to describe. The child
organisations that used them were removed in 0034.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0034_remove_department_child_orgs"),
        ("catalog", "0012_rename_the_two_type_models"),
    ]

    operations = [
        migrations.RenameField(
            model_name="organisation",
            old_name="group_type", new_name="organisation_type",
        ),
        migrations.RemoveField(model_name="organisation", name="department_type"),
        migrations.RemoveField(model_name="organisation", name="department_label"),
    ]
