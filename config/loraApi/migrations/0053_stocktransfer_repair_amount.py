from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0052_stocktransfer_last_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='repair_amount',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]
