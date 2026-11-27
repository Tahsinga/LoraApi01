from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0050_stocktransfer_adjustment_report'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='repair_of',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
