from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0051_stocktransfer_repair_of'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='last_error',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
