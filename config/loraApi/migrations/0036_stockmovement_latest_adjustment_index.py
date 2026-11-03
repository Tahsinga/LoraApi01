from django.db import migrations, models
from django.db.models.functions import Upper


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0035_product_sync_updated_at_indexes'),
    ]

    operations = [
        # Lets the stock summary find each product's latest adjustment with one
        # index probe instead of sorting every adjusted/stocktake/maintained row.
        migrations.AddIndex(
            model_name='stockmovement',
            index=models.Index(
                Upper('branch'), 'product_id', models.F('created_at').desc(), models.F('id').desc(),
                name='sm_latest_adjustment_idx',
                condition=models.Q(movement_type__in=['adjusted', 'stocktake', 'maintained']),
            ),
        ),
    ]
