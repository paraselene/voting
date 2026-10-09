from django.db import migrations


def create_election(apps, schema_editor):
    apps.get_model('election', 'Election').objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('election', '0001_initial')]
    operations = [migrations.RunPython(create_election, migrations.RunPython.noop)]
