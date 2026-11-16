import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Record website renames and which branches have received them.

    Only creates new tables; existing tables and rows are not touched.
    """

    dependencies = [
        ('loraApi', '0039_repair_main_product_renames'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductRename',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_id', models.IntegerField()),
                ('old_name', models.CharField(blank=True, default='', max_length=250)),
                ('new_name', models.CharField(max_length=250)),
                ('renamed_by', models.CharField(blank=True, default='', max_length=255)),
                ('renamed_at', models.DateTimeField()),
            ],
            options={
                'ordering': ['-renamed_at', '-id'],
                'indexes': [
                    models.Index(fields=['product_id', '-renamed_at'], name='product_rename_product_idx'),
                    models.Index(fields=['-renamed_at'], name='product_rename_time_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProductRenameBranch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=255)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('rename', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='branches', to='loraApi.productrename')),
            ],
            options={
                'ordering': ['branch'],
                'indexes': [models.Index(fields=['branch', 'confirmed_at'], name='product_rename_branch_idx')],
                'constraints': [models.UniqueConstraint(fields=('rename', 'branch'), name='unique_product_rename_branch')],
            },
        ),
    ]
