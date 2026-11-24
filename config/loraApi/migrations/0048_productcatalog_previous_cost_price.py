from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0047_productcatalog_cost_price'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='previous_cost_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
    ]
