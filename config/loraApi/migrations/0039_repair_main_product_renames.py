from django.db import migrations


def repair_main_rows(apps, schema_editor):
    """Put website renames back on MAIN rows the Main POS publish overwrote.

    Before this fix a rename left the MAIN row unprotected, so the Main POS's
    next catalog publish restored the old name there. Where a branch row still
    holds the website name (pending_product_name), copy it to the MAIN row and
    protect that row too.
    """
    ProductCatalog = apps.get_model('loraApi', 'ProductCatalog')
    MainStockBalance = apps.get_model('loraApi', 'MainStockBalance')

    renamed = {}
    for product_id, name in (
        ProductCatalog.objects.exclude(branch__iexact='MAIN')
        .exclude(pending_product_name='')
        .order_by('product_id', '-updated_at')
        .values_list('product_id', 'pending_product_name')
    ):
        renamed.setdefault(product_id, name)

    for product_id, name in renamed.items():
        ProductCatalog.objects.filter(branch__iexact='MAIN', product_id=product_id).update(
            product_name=name,
            pending_product_name=name,
        )
        MainStockBalance.objects.filter(product_id=product_id).update(product_name=name)


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0038_deletedproductsighting'),
    ]

    operations = [
        migrations.RunPython(repair_main_rows, migrations.RunPython.noop),
    ]
