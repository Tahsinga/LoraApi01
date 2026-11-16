from django.db import migrations
from django.utils import timezone

# Old and test branches that will never take renames. Only rename tracking
# changes; their stock, history and POS sync are untouched. Each can be added
# back on the Renamed products page.
OLD_BRANCHES = ['CROSSCOUNTRY', 'LAXDOM TEST', 'TEST']


def remove_old_branches(apps, schema_editor):
    RenameIgnoredBranch = apps.get_model('loraApi', 'RenameIgnoredBranch')
    now = timezone.now()
    for branch in OLD_BRANCHES:
        if not RenameIgnoredBranch.objects.filter(branch__iexact=branch).exists():
            RenameIgnoredBranch.objects.create(branch=branch, removed_by='setup', removed_at=now)


def add_old_branches_back(apps, schema_editor):
    RenameIgnoredBranch = apps.get_model('loraApi', 'RenameIgnoredBranch')
    RenameIgnoredBranch.objects.filter(branch__in=OLD_BRANCHES, removed_by='setup').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0041_renameignoredbranch'),
    ]

    operations = [
        migrations.RunPython(remove_old_branches, add_old_branches_back),
    ]
