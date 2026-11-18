from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.db import migrations
from django.db.models import Q

# Mini Market returned these two invoices on 18 Nov 2026 for sales made on 17 Nov 2026.
# Move the return date shown on the website back one day so it matches the sale day
# (the branch POS database is changed with branch-scripts/MiniMarket-move-returns-to-2026-11-17.sql).
BRANCH = 'Mini Market'
INVOICES = ['1050573', '1050423']
LOCAL_TIME = ZoneInfo('Africa/Harare')
RETURN_DAY = date(2026, 11, 18)
SALE_DAY = date(2026, 11, 17)


def shift_dates(apps, from_day, days):
	DeletionRecord = apps.get_model('loraApi', 'DeletionRecord')
	records = DeletionRecord.objects.filter(invoice__in=INVOICES).filter(
		Q(branch__iexact=BRANCH) | Q(confirmed_branch__iexact=BRANCH)
	)
	for record in records:
		changed = []
		for field in ('timestamp', 'confirmation_timestamp'):
			value = getattr(record, field)
			if value and value.astimezone(LOCAL_TIME).date() == from_day:
				setattr(record, field, value + timedelta(days=days))
				changed.append(field)
		if changed:
			# update() so auto_now_add on timestamp does not replace the new value.
			DeletionRecord.objects.filter(pk=record.pk).update(**{field: getattr(record, field) for field in changed})


def move_to_sale_day(apps, schema_editor):
	shift_dates(apps, RETURN_DAY, -1)


def move_back_to_return_day(apps, schema_editor):
	shift_dates(apps, SALE_DAY, 1)


class Migration(migrations.Migration):

	dependencies = [
		('loraApi', '0045_productrenamebranch_forced_by'),
	]

	operations = [
		migrations.RunPython(move_to_sale_day, move_back_to_return_day),
	]
