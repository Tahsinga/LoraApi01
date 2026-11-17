from django.db import migrations
from django.utils import timezone

# Test branches still pending on the deletion history. Removed branches are left out
# of renames and deletions; their stock, history and POS sync are untouched. Each can
# be added back on the Renamed products page.
TEST_BRANCHES = ['BranchA', 'Pangoline', 'TASHINGA']


def remove_test_branches(apps, schema_editor):
    RenameIgnoredBranch = apps.get_model('loraApi', 'RenameIgnoredBranch')
    now = timezone.now()
    for branch in TEST_BRANCHES:
        if not RenameIgnoredBranch.objects.filter(branch__iexact=branch).exists():
            RenameIgnoredBranch.objects.create(branch=branch, removed_by='setup', removed_at=now)


def add_test_branches_back(apps, schema_editor):
    RenameIgnoredBranch = apps.get_model('loraApi', 'RenameIgnoredBranch')
    RenameIgnoredBranch.objects.filter(branch__in=TEST_BRANCHES, removed_by='setup').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0042_remove_old_branches_from_renames'),
    ]

    operations = [
        migrations.RunPython(remove_test_branches, add_test_branches_back),
    ]
