from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('loraApi', '0044_transfer_movement_done_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='productrenamebranch',
            name='forced_by',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
