from django.db import migrations, models


class Migration(migrations.Migration):
    """Track deleted products that a branch POS still has active.

    Only creates a new table; existing tables and rows are not touched.
    """

    dependencies = [
        ('loraApi', '0037_product_rename_and_restore'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeletedProductSighting',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=255)),
                ('product_id', models.IntegerField()),
                ('product_name', models.CharField(blank=True, default='', max_length=250)),
                ('available_quantity', models.DecimalField(decimal_places=0, default=0, max_digits=18)),
                ('last_seen', models.DateTimeField()),
            ],
            options={
                'ordering': ['product_id', 'branch'],
                'indexes': [models.Index(fields=['product_id'], name='deleted_sighting_product_idx')],
                'constraints': [models.UniqueConstraint(fields=('branch', 'product_id'), name='unique_deleted_product_sighting')],
            },
        ),
    ]
