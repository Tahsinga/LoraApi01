from django.db import migrations, models


class Migration(migrations.Migration):
    """Product name editing and restoring deleted products.

    Only adds columns (all with defaults) and a new status choice, so it is safe to
    run on a live database and does not touch existing rows.
    """

    dependencies = [
        ('loraApi', '0036_stockmovement_latest_adjustment_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='pending_product_name',
            field=models.CharField(blank=True, default='', max_length=250),
        ),
        migrations.AddField(
            model_name='productdeletionrequest',
            name='product_code',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='productdeletionrequest',
            name='barcode',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='productdeletionrequest',
            name='selling_price',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name='productdeletionrequest',
            name='tax_rate',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AlterField(
            model_name='productdeletionrequest',
            name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed'), ('restored', 'Restored')],
                default='pending',
                max_length=20,
            ),
        ),
    ]
