from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0049_productcatalog_pos_movement_balance'),
    ]

    operations = [
        migrations.AddField(
            model_name='stocktransfer',
            name='before_branch_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='stocktransfer',
            name='before_movement_balance',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='stocktransfer',
            name='after_branch_quantity',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='stocktransfer',
            name='after_movement_balance',
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=18, null=True),
        ),
    ]
