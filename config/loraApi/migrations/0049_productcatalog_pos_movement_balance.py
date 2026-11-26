from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0048_productcatalog_previous_cost_price'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='pos_movement_balance',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]
