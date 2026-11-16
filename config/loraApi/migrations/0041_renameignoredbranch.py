from django.db import migrations, models


class Migration(migrations.Migration):
    """Branches left out of rename tracking. Only creates a new table."""

    dependencies = [
        ('loraApi', '0040_product_rename_tracking'),
    ]

    operations = [
        migrations.CreateModel(
            name='RenameIgnoredBranch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=255, unique=True)),
                ('removed_by', models.CharField(blank=True, default='', max_length=255)),
                ('removed_at', models.DateTimeField()),
            ],
            options={'ordering': ['branch']},
        ),
    ]
