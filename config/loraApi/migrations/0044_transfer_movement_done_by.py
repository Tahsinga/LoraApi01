from datetime import timedelta

from django.db import migrations

OLD_SOURCE = 'Web stock transfer'
MATCH_WINDOW = timedelta(minutes=10)


def name_transfer_movements(apps, schema_editor):
    """Old transfer movements were saved as 'Web stock transfer' with no user.

    Match each one to its completed transfer (same branch, product and quantity,
    completed close to when the movement was saved) and save who sent it.
    """
    StockMovement = apps.get_model('loraApi', 'StockMovement')
    StockTransfer = apps.get_model('loraApi', 'StockTransfer')
    used = set()
    for movement in StockMovement.objects.filter(source=OLD_SOURCE, movement_type='received').order_by('created_at', 'id').iterator():
        candidates = StockTransfer.objects.filter(
            branch__iexact=movement.branch,
            product_id=movement.product_id,
            quantity=movement.quantity,
            status='completed',
            target_quantity__isnull=True,
            created_at__lte=movement.created_at + timedelta(minutes=1),
            completed_at__gte=movement.created_at - MATCH_WINDOW,
            completed_at__lte=movement.created_at + MATCH_WINDOW,
        ).exclude(transfer_id__in=used)
        best = min(candidates, key=lambda item: abs(item.completed_at - movement.created_at), default=None)
        if best is None:
            continue
        used.add(best.transfer_id)
        movement.source = f'{best.created_by or "System"} (transfer to {best.branch})'
        movement.save(update_fields=['source'])


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0043_remove_test_branches_from_deletions'),
    ]

    operations = [
        migrations.RunPython(name_transfer_movements, migrations.RunPython.noop),
    ]
