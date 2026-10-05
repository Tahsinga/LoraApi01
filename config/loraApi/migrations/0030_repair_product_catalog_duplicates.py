from django.db import migrations
from django.db.models import Count


def repair_product_catalog_duplicates(apps, schema_editor):
    ProductCatalog = apps.get_model('loraApi', 'ProductCatalog')
    duplicate_groups = list(
        ProductCatalog.objects.values('branch', 'product_id')
        .annotate(row_count=Count('pk'))
        .filter(row_count__gt=1)
    )

    for group in duplicate_groups:
        matching_catalogs = list(ProductCatalog.objects.filter(
            branch=group['branch'],
            product_id=group['product_id'],
        ).order_by('-updated_at', '-pk'))
        catalog = matching_catalogs[0]
        merged_fields = set()
        for duplicate in matching_catalogs[1:]:
            if duplicate.pending_stock_adjustment and not catalog.pending_stock_adjustment:
                catalog.pending_stock_adjustment = True
                catalog.pending_stock_quantity = duplicate.pending_stock_quantity
                merged_fields.update(('pending_stock_adjustment', 'pending_stock_quantity'))
            if duplicate.pending_price_update and not catalog.pending_price_update:
                catalog.pending_price_update = True
                catalog.pending_selling_price = duplicate.pending_selling_price
                merged_fields.update(('pending_price_update', 'pending_selling_price'))
            if duplicate.sold_quantity is not None and (
                catalog.sold_quantity is None or duplicate.sold_quantity > catalog.sold_quantity
            ):
                catalog.sold_quantity = duplicate.sold_quantity
                merged_fields.add('sold_quantity')
            if duplicate.branch_confirmed and not catalog.branch_confirmed:
                catalog.branch_confirmed = True
                merged_fields.add('branch_confirmed')
            if duplicate.pending_product_creation and not catalog.pending_product_creation:
                catalog.pending_product_creation = True
                merged_fields.add('pending_product_creation')

        if merged_fields:
            catalog.save(update_fields=sorted(merged_fields))
        ProductCatalog.objects.filter(pk__in=[item.pk for item in matching_catalogs[1:]]).delete()

    table_name = ProductCatalog._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(cursor, table_name)
    has_unique_branch_product = any(
        constraint.get('unique') and constraint.get('columns') == ['branch', 'product_id']
        for constraint in constraints.values()
    )
    if not has_unique_branch_product:
        unique_constraint = next(
            constraint
            for constraint in ProductCatalog._meta.constraints
            if constraint.name == 'unique_branch_product'
        )
        schema_editor.add_constraint(ProductCatalog, unique_constraint)


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0029_productdeletionrequest'),
    ]

    operations = [
        migrations.RunPython(repair_product_catalog_duplicates, migrations.RunPython.noop),
    ]