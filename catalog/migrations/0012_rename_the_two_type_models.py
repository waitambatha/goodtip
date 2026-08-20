"""Rename the two type tables so each word means one thing.

The vocabulary was crossed. `GroupType` held the ORGANISATION type — Business,
Community, Education, Charities, Informal — while `DepartmentType` held the
department kind: IT, Finance, Marketing. Under the new vocabulary a department
is a group, so `DepartmentType` is what "group type" should mean, and the name
was taken by something else entirely.

Leaving it would have meant `Group.kind` pointing at `DepartmentType` while
`Organisation.group_type` meant something unrelated — two live meanings of
"group type" in one schema, which is the sort of thing that reads fine to
whoever wrote it and is a trap for everyone after.

RenameModel/RenameField, never drop-and-create: these tables hold real rows
(every organisation's type, every sub-category's parent) and makemigrations
cannot always tell a rename from a delete plus an add. Written by hand so it
cannot guess wrong.

Order matters. The organisation type has to vacate the name GroupType before
the department type can take it.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0011_competitions_for_every_season"),
        # Group.kind is declared against catalog.DepartmentType, so that model
        # has to still exist under its old name when orgs.0033 is applied.
        # Without this, the graph is free to run this rename first and building
        # the historical state fails with "Related model
        # 'catalog.departmenttype' cannot be resolved" — on a fresh database
        # only, which means every test run and every new environment.
        ("orgs", "0034_remove_department_child_orgs"),
    ]

    operations = [
        # 1. GroupType (the organisation type) becomes OrganisationType.
        migrations.RenameModel(
            old_name="GroupType", new_name="OrganisationType",
        ),
        # 2. Its name is now free for the department type.
        migrations.RenameModel(
            old_name="DepartmentType", new_name="GroupType",
        ),
        # 3. The FKs that pointed at "the organisation type" under its old name.
        migrations.RenameField(
            model_name="subcategory",
            old_name="group_type", new_name="organisation_type",
        ),
        migrations.RenameField(
            model_name="grouptype",          # the ex-DepartmentType
            old_name="group_type", new_name="organisation_type",
        ),
    ]
