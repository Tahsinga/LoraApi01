from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0031_product_sync_lookup_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='pos_movement_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='productcatalog',
            name='pos_sold_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='productcatalog',
            name='pos_received_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]