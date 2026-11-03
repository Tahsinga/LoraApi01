from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0034_stocktransfer_retry_backoff'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='productcatalog',
            index=models.Index(fields=['updated_at'], name='pc_updated_at_idx'),
        ),
        migrations.AddIndex(
            model_name='productdeletionrequest',
            index=models.Index(fields=['product_id'], name='proddelete_product_idx'),
        ),
    ]
