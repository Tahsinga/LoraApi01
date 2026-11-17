from datetime import timedelta

from django.db import migrations, transaction

OLD_SOURCE = 'Web stock transfer'
MATCH_WINDOW = timedelta(minutes=10)
BATCH_SIZE = 500


def name_transfer_movements(apps, schema_editor):
    """Old transfer movements were saved as 'Web stock transfer' with no user.

    Match each one to its completed transfer (same branch, product and quantity,
    completed close to when the movement was saved) and save who sent it.
    Everything is loaded once and matched in memory, then saved in small batches
    that each commit straight away, so the live site is never kept waiting.
    Safe to run again: only rows still marked 'Web stock transfer' are touched.
    """
    StockMovement = apps.get_model('loraApi', 'StockMovement')
    StockTransfer = apps.get_model('loraApi', 'StockTransfer')

    movements = list(
        StockMovement.objects.filter(source=OLD_SOURCE, movement_type='received')
        .order_by('created_at', 'id')
        .only('id', 'branch', 'product_id', 'quantity', 'created_at')
    )
    if not movements:
        return

    transfers_by_key = {}
    for transfer in StockTransfer.objects.filter(
        status='completed',
        target_quantity__isnull=True,
        completed_at__isnull=False,
        product_id__in={item.product_id for item in movements},
    ).only('transfer_id', 'branch', 'product_id', 'quantity', 'created_by', 'created_at', 'completed_at').iterator():
        key = (transfer.branch.casefold(), transfer.product_id, transfer.quantity)
        transfers_by_key.setdefault(key, []).append(transfer)

    used = set()
    changed = []
    for movement in movements:
        candidates = [
            item for item in transfers_by_key.get((movement.branch.casefold(), movement.product_id, movement.quantity), [])
            if item.transfer_id not in used
            and item.created_at <= movement.created_at + timedelta(minutes=1)
            and abs(item.completed_at - movement.created_at) <= MATCH_WINDOW
        ]
        best = min(candidates, key=lambda item: abs(item.completed_at - movement.created_at), default=None)
        if best is None:
            continue
        used.add(best.transfer_id)
        changed.append((movement.id, f'{best.created_by or "System"} (transfer to {best.branch})'))

    for start in range(0, len(changed), BATCH_SIZE):
        with transaction.atomic():
            for movement_id, source in changed[start:start + BATCH_SIZE]:
                StockMovement.objects.filter(pk=movement_id, source=OLD_SOURCE).update(source=source)


class Migration(migrations.Migration):
    # Commit each batch on its own instead of one long transaction over the live table.
    atomic = False

    dependencies = [
        ('loraApi', '0043_remove_test_branches_from_deletions'),
    ]

    operations = [
        migrations.RunPython(name_transfer_movements, migrations.RunPython.noop, atomic=False),
    ]
