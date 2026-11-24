from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0046_mini_market_returns_to_sale_day'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalog',
            name='cost_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='productcatalog',
            name='pending_cost_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
    ]
