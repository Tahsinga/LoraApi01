import hashlib
import hmac
import json
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import SetPasswordForm, UserCreationForm
from django.core.cache import cache
from django.db import OperationalError, ProgrammingError, close_old_connections, connection, transaction
from django.db.models import Case, F, IntegerField, Max, OuterRef, Q, Subquery, Sum, When
from django.db.models.functions import Cast
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.views.decorators.csrf import csrf_exempt

from .models import BranchHeartbeat, DeletionRecord, InvoiceReprintRequest, MainStockBalance, ProductCatalog, ProductDeletionRequest, SalesReportRequest, SalesReportSchedule, StockMovement, StockTransfer
from .state_store import sync_users_to_state

WHATSAPP_STOCK_BRANCHES = (
    'COMPLEX',
    'CROSS COUNTRY',
    'DZOVANI',
    'INVESTMENTS',
    'Mini Market',
    'PERFECT',
    'SISK',
    'The Place',
    'VEES',
    'WEST',
    'YEUKAI',
    'ZBS',
)

"""
================================================================================
LORA POS RETURNS - DELETION TRIGGER SYSTEM
================================================================================

This API acts as a trigger mediator for invoice deletions across branch and main
.databases. It ensures consistent deletion of the SAME invoice across all systems.

FLOW:
1. Branch cancels invoice locally → Sends complete details to /api/branch-sync/ (POST)
2. Main cancels invoice → Sends complete details to /api/main-sync/ (POST)
3. API stores deletion trigger with all required fields (invoice, product_id, entry_no, branch)
4. Branch polls /api/branch-sync/?branch=X for pending deletions (GET)
5. Branch executes DELETE using exact field values from trigger
6. Branch confirms deletion → Sends to /api/confirm-deletion/ (POST)
7. API tracks completion

KEY: All deletion triggers include COMPLETE data needed for exact WHERE clause match:
- invoice (required)
- product_id (optional but recommended)
- entry_no (optional but recommended)
- branch (required)

This ensures the SAME invoice number that was cancelled is deleted with 100% accuracy.
================================================================================
"""

BRANCH_ONLINE_SECONDS = 300
CONNECTED_BRANCHES = {}
BRANCH_HEARTBEAT_DB_AVAILABLE = True
PRODUCT_SYNC_QUEUE = Lock()
PRODUCT_CACHE_VERSION_KEY = 'lora:product-cache-version'
PRODUCT_CACHE_SECONDS = 15


def invalidate_product_catalog_cache():
    version = cache.get(PRODUCT_CACHE_VERSION_KEY, 0)
    cache.set(PRODUCT_CACHE_VERSION_KEY, int(version) + 1, None)


def get_or_create_branch_catalog(branch, product_id, defaults):
    catalogs = list(
        ProductCatalog.objects.select_for_update()
        .filter(branch__iexact=branch, product_id=product_id)
        .order_by('-updated_at', '-pk')
    )
    if not catalogs:
        return ProductCatalog.objects.select_for_update().get_or_create(
            branch=branch,
            product_id=product_id,
            defaults=defaults,
        )

    catalog = next((item for item in catalogs if item.branch == branch), catalogs[0])
    for duplicate in catalogs:
        if duplicate.pk == catalog.pk:
            continue
        if duplicate.pending_stock_adjustment and not catalog.pending_stock_adjustment:
            catalog.pending_stock_adjustment = True
            catalog.pending_stock_quantity = duplicate.pending_stock_quantity
        if duplicate.pending_price_update and not catalog.pending_price_update:
            catalog.pending_price_update = True
            catalog.pending_selling_price = duplicate.pending_selling_price
        if duplicate.sold_quantity is not None and (
            catalog.sold_quantity is None or duplicate.sold_quantity > catalog.sold_quantity
        ):
            catalog.sold_quantity = duplicate.sold_quantity
        catalog.branch_confirmed = catalog.branch_confirmed or duplicate.branch_confirmed
        catalog.pending_product_creation = catalog.pending_product_creation or duplicate.pending_product_creation

    duplicate_ids = [item.pk for item in catalogs if item.pk != catalog.pk]
    if duplicate_ids:
        ProductCatalog.objects.filter(pk__in=duplicate_ids).delete()
    if catalog.branch != branch:
        catalog.branch = branch
    return catalog, False


def retry_on_database_lock(view_func):
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        sync_lock = PRODUCT_SYNC_QUEUE if connection.vendor == 'sqlite' else nullcontext()
        with sync_lock:
            for attempt in range(5):
                try:
                    close_old_connections()
                    return view_func(request, *args, **kwargs)
                except OperationalError as error:
                    message = str(error).lower()
                    transient_error = any(
                        marker in message
                        for marker in (
                            'locked',
                            'server closed the connection',
                            'connection refused',
                            'connection is closed',
                            'terminating connection',
                        )
                    )
                    if not transient_error or attempt == 4:
                        raise
                    close_old_connections()
                    time.sleep(1.0 * (attempt + 1))

    return wrapped_view


def whole_quantity(value):
    return value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def cleanup_queues():
    """Keep deletion records available as a permanent cancellation history."""
    return None


def record_payload(record):
    return {
        'id': record.deletion_id,
        'branch': record.branch,
        'invoice': record.invoice,
        'product_id': record.product_id,
        'entry_no': record.entry_no,
        'action': record.action,
        'status': record.status,
        'timestamp': record.timestamp.timestamp(),
        'source': record.source,
        'deleted_from_main': record.deleted_from_main,
        'message': record.message,
        'deleted_rows': record.deleted_rows,
        'deleted_by': record.deleted_by,
        'confirmed_branch': record.confirmed_branch,
        'confirmation_timestamp': record.confirmation_timestamp.timestamp() if record.confirmation_timestamp else None,
    }


def report_payload(report):
    return {
        'id': report.request_id,
        'branch': report.branch,
        'report_date': report.report_date.isoformat(),
        'scheduled_at': report.scheduled_at.isoformat() if report.scheduled_at else None,
        'status': report.status,
        'requested_at': report.requested_at.timestamp(),
        'completed_at': report.completed_at.timestamp() if report.completed_at else None,
        'row_count': report.row_count,
        'error': report.error_message,
    }


def queue_due_sales_reports(branch_name=None):
    current_time = timezone.now()
    with transaction.atomic():
        if branch_name:
            schedules = SalesReportSchedule.objects.select_for_update().filter(branch__iexact=branch_name)
        else:
            schedules = SalesReportSchedule.objects.select_for_update().all()

        for schedule in schedules:
            try:
                local_now = current_time.astimezone(ZoneInfo(schedule.timezone))
            except (ZoneInfoNotFoundError, ValueError):
                continue

            local_date = local_now.date()
            if local_now.time().replace(tzinfo=None) < schedule.report_time or schedule.last_queued_date == local_date:
                continue

            report_id = f'REPORT_DAILY_{schedule.pk}_{local_date.isoformat()}'
            SalesReportRequest.objects.get_or_create(
                request_id=report_id,
                defaults={
                    'branch': schedule.branch,
                    'report_date': local_date,
                    'status': 'pending',
                    'requested_by': 'daily_schedule',
                },
            )
            schedule.last_queued_date = local_date
            schedule.save(update_fields=['last_queued_date', 'updated_at'])

        SalesReportRequest.objects.filter(
            status='scheduled',
            scheduled_at__lte=current_time,
        ).update(status='pending')


def transfer_payload(transfer):
    return {
        'id': transfer.transfer_id,
        'branch': transfer.branch,
        'product_id': transfer.product_id,
        'product_name': transfer.product_name,
        'quantity': str(transfer.quantity),
		'target_quantity': str(transfer.target_quantity) if transfer.target_quantity is not None else None,
        'status': transfer.status,
        'created_at': transfer.created_at.timestamp(),
    }


def product_payload(product):
    return {
        'branch': product.branch,
        'product_id': product.product_id,
        'product_name': product.product_name,
        'product_code': product.product_code,
        'barcode': product.barcode,
        'available_quantity': str(product.available_quantity),
        'selling_price': str(product.selling_price),
        'tax_rate': str(product.tax_rate),
    }


def sum_quantities(queryset):
    return Decimal(queryset.aggregate(total=Sum(Cast('quantity', IntegerField())))['total'] or 0)


def stock_summary_payload(product, branch):
    balance = MainStockBalance.objects.filter(product_id=product.product_id).first()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    received = sum_quantities(StockMovement.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        movement_type='received',
        created_at__gte=today_start,
        created_at__lt=tomorrow_start,
    ))
    sold = sum_quantities(StockMovement.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        movement_type='sold',
        created_at__gte=today_start,
        created_at__lt=tomorrow_start,
    ))
    sent = sum_quantities(StockTransfer.objects.filter(
        product_id=product.product_id,
        branch__iexact=branch,
        status__in=['pending', 'processing', 'completed'],
    ))
    available = product.available_quantity
    return {
        **product_payload(product),
        'main_quantity': str(balance.quantity if balance else Decimal('0')),
        'received_quantity': str(received),
        'sent_quantity': str(sent),
        'sold_quantity': str(sold),
        'available_quantity': str(available),
    }


@login_required(login_url='/login/')
@csrf_exempt
def index(request):
    """Browser dashboard for managing branch sale cancellations."""
    cleanup_queues()
    response = render(request, 'loraApi/dashboard.html', {
        'pending_count': DeletionRecord.objects.filter(status__in=['pending', 'processing']).count(),
        'processed_count': DeletionRecord.objects.filter(status='processed').count(),
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    return response


@login_required(login_url='/login/')
def main_stock(request):
    """Show the central stock adjustment and branch transfer page."""
    response = render(request, 'loraApi/main_stock.html')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response['Pragma'] = 'no-cache'
    return response


@login_required(login_url='/login/')
def deleted_products_page(request):
    return render(request, 'loraApi/deleted_products.html')


@login_required(login_url='/login/')
def product_movement_history(request):
    """Show the searchable, date-filtered stock movement audit page."""
    return render(request, 'loraApi/product_movements.html')


@login_required(login_url='/login/')
def movement_adjustments_page(request):
    return render(request, 'loraApi/movement_adjustments.html')


@login_required(login_url='/login/')
def bandwidth_usage(request):
    """Show browser-local API response bandwidth totals."""
    return render(request, 'loraApi/bandwidth.html')


@login_required(login_url='/login/')
@csrf_exempt
def cancellation_history(request):
    """Show the saved cancellation history page."""
    return render(request, 'loraApi/history.html')


@login_required(login_url='/login/')
@csrf_exempt
def cancellation_history_api(request):
    """Return cancellation requests in newest-first order."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = request.GET.get('branch', '').strip()
    invoice = request.GET.get('invoice', '').strip()
    query = DeletionRecord.objects.filter(status__in=['pending', 'processing', 'processed'])
    if branch:
        query = query.filter(branch__icontains=branch)
    if invoice:
        query = query.filter(invoice__icontains=invoice)

    records = list(query.order_by('-confirmation_timestamp', '-timestamp')[:500])
    branches = list(
        DeletionRecord.objects.filter(status__in=['pending', 'processing', 'processed'])
        .exclude(branch='')
        .values_list('branch', flat=True)
        .distinct()
        .order_by('branch')
    )
    return JsonResponse({
        'status': 'ok',
        'count': len(records),
        'branches': branches,
        'cancellations': [
            {
                'id': record.deletion_id,
                'invoice': record.invoice,
                'branch': record.confirmed_branch or record.branch,
                'product_id': record.product_id,
                'entry_no': record.entry_no,
                'cancelled_at': (record.confirmation_timestamp or record.timestamp).isoformat(),
                'deleted_rows': record.deleted_rows,
                'deleted_by': record.deleted_by or 'Not reported',
                'source': record.source,
                'status': record.status,
                'message': record.message,
                'products': json.loads(record.receipt_products or '[]'),
                'total': str(record.receipt_total) if record.receipt_total is not None else '0.00',
            }
            for record in records
        ],
    })


def is_admin(user):
    return user.is_authenticated and user.is_superuser


@user_passes_test(is_admin, login_url='/login/')
def user_management(request):
    User = get_user_model()
    message = ''
    error = ''

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            form = UserCreationForm(request.POST)
            if form.is_valid():
                password = form.cleaned_data['password1']
                user = form.save()
                user.is_staff = True
                user.is_superuser = False
                user.save(update_fields=['is_staff', 'is_superuser'])
                sync_users_to_state({user.username: password})
                message = f'User {user.username} was created.'
            else:
                error = ' '.join(
                    message for messages in form.errors.values() for message in messages
                )
        elif action == 'change_password':
            try:
                user = User.objects.get(pk=request.POST.get('user_id'))
            except User.DoesNotExist:
                error = 'User not found.'
            else:
                form = SetPasswordForm(user, request.POST)
                if form.is_valid():
                    password = form.cleaned_data['new_password1']
                    form.save()
                    sync_users_to_state({user.username: password})
                    if user == request.user:
                        update_session_auth_hash(request, user)
                    message = f'Password changed for {user.username}.'
                else:
                    error = 'Password change failed. Use matching passwords and meet the password rules.'
        elif action == 'delete':
            try:
                user = User.objects.get(pk=request.POST.get('user_id'))
            except User.DoesNotExist:
                error = 'User not found.'
            else:
                if user == request.user:
                    error = 'The active Admin account cannot be deleted.'
                else:
                    username = user.username
                    user.delete()
                    sync_users_to_state()
                    message = f'User {username} was deleted.'

    users = User.objects.order_by('username')
    return render(request, 'loraApi/users.html', {
        'users': users,
        'message': message,
        'error': error,
    })


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def cancel_sale(request):
    """Queue a sale cancellation requested from the browser dashboard."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = str(payload.get('invoice', '')).strip()
    branch = str(payload.get('branch', '')).strip()
    product_id = payload.get('product_id')

    if not invoice or not branch:
        return JsonResponse({'status': 'error', 'message': 'Invoice and branch are required.'}, status=400)

    deletion_id = f"WEB_{branch}_{invoice}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=branch,
        invoice=invoice,
        product_id=product_id,
        entry_no='',
        action='cancel_invoice',
        status='pending',
        source='web_dashboard',
        deleted_by=request.user.get_username(),
        message=f'Web cancellation requested for invoice {invoice} at {branch}',
    )

    return JsonResponse({'status': 'accepted', 'message': f'Cancellation queued for invoice {invoice}.', 'deletion_id': deletion_record.deletion_id}, status=202)


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def request_sales_report(request):
    """Queue a Movement sales report for printing on a connected branch PC."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    requested_branches = payload.get('branches')
    if requested_branches is None:
        requested_branches = [payload.get('branch', '')]
    if not isinstance(requested_branches, list):
        requested_branches = [requested_branches]

    branches = [str(branch).strip() for branch in requested_branches if str(branch).strip()]
    if payload.get('all_branches'):
        branches = [branch['name'] for branch in CONNECTED_BRANCHES.values()]
    branches = sorted(set(branches), key=str.casefold)
    report_date = parse_date(str(payload.get('report_date', '')).strip())
    scheduled_at_value = payload.get('scheduled_at')
    scheduled_at = parse_datetime(str(scheduled_at_value)) if scheduled_at_value else None
    if scheduled_at_value and (scheduled_at is None or timezone.is_naive(scheduled_at)):
        return JsonResponse({'status': 'error', 'message': 'Choose a valid local print time.'}, status=400)
    if scheduled_at and scheduled_at <= timezone.now():
        return JsonResponse({'status': 'error', 'message': 'Print time must be in the future.'}, status=400)
    if not branches or report_date is None:
        return JsonResponse({'status': 'error', 'message': 'Select at least one branch and a valid report date.'}, status=400)

    reports = []
    for index, branch in enumerate(branches):
        request_id = f"REPORT_{branch}_{report_date.isoformat()}_{int(time.time() * 1000)}_{index}"
        reports.append(SalesReportRequest.objects.create(
            request_id=request_id,
            branch=branch,
            report_date=report_date,
            scheduled_at=scheduled_at,
            status='scheduled' if scheduled_at else 'pending',
            requested_by=request.user.get_username(),
        ))
    return JsonResponse({
        'status': 'accepted',
        'message': (
            f'Sales reports scheduled for {len(reports)} branch(es) at {timezone.localtime(scheduled_at).strftime("%Y-%m-%d %H:%M")}.'
            if scheduled_at else f'Sales reports queued for {len(reports)} branch(es) on {report_date.isoformat()}.'
        ),
        'reports': [report_payload(report) for report in reports],
    }, status=202)


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def sales_report_schedules(request):
    if request.method == 'GET':
        schedules = SalesReportSchedule.objects.all()
        return JsonResponse({
            'status': 'ok',
            'schedules': [
                {
                    'branch': schedule.branch,
                    'time': schedule.report_time.strftime('%H:%M'),
                    'timezone': schedule.timezone,
                }
                for schedule in schedules
            ],
        })

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use GET or POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    schedule_rows = payload.get('schedules')
    if not isinstance(schedule_rows, list):
        return JsonResponse({'status': 'error', 'message': 'Schedules must be a list.'}, status=400)

    normalized_schedules = {}
    for row in schedule_rows:
        branch = str(row.get('branch', '')).strip() if isinstance(row, dict) else ''
        report_time = parse_time(str(row.get('time', '')).strip()) if isinstance(row, dict) else None
        time_zone = str(row.get('timezone', '')).strip() if isinstance(row, dict) else ''
        if not branch or report_time is None or report_time.tzinfo is not None or not time_zone:
            return JsonResponse({'status': 'error', 'message': 'Each schedule needs a branch, local time, and timezone.'}, status=400)
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            return JsonResponse({'status': 'error', 'message': f'Unknown timezone for {branch}.'}, status=400)
        branch_key = branch.casefold()
        if branch_key in normalized_schedules:
            return JsonResponse({'status': 'error', 'message': f'{branch} is listed more than once.'}, status=400)
        normalized_schedules[branch_key] = (branch, report_time, time_zone)

    with transaction.atomic():
        for branch, report_time, time_zone in normalized_schedules.values():
            schedule = SalesReportSchedule.objects.filter(branch__iexact=branch).first()
            if schedule is None:
                SalesReportSchedule.objects.create(
                    branch=branch,
                    report_time=report_time,
                    timezone=time_zone,
                )
            else:
                schedule.branch = branch
                schedule.report_time = report_time
                schedule.timezone = time_zone
                schedule.save(update_fields=['branch', 'report_time', 'timezone', 'updated_at'])

        for schedule in SalesReportSchedule.objects.all():
            if schedule.branch.casefold() not in normalized_schedules:
                schedule.delete()

    return JsonResponse({
        'status': 'ok',
        'message': f'Daily print times saved for {len(normalized_schedules)} branch(es).',
        'count': len(normalized_schedules),
    })


@login_required(login_url='/login/')
@csrf_exempt
def request_invoice_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        invoice = str(payload.get('invoice', '')).strip()
        branch = str(payload.get('branch', '')).strip()
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    if not invoice or not branch:
        return JsonResponse({'status': 'error', 'message': 'Invoice number and branch are required.'}, status=400)
    request_id = f"REPRINT_{branch}_{invoice}_{int(time.time() * 1000)}"
    reprint = InvoiceReprintRequest.objects.create(
        request_id=request_id,
        branch=branch,
        invoice=invoice,
        requested_by=request.user.get_username(),
    )
    return JsonResponse({'status': 'accepted', 'message': f'Invoice {invoice} reprint queued for {branch}.', 'request_id': reprint.request_id}, status=202)


@csrf_exempt
@retry_on_database_lock
def complete_invoice_reprint(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        request_id = str(payload.get('request_id', '')).strip()
        success = bool(payload.get('success'))
        error = str(payload.get('error', '')).strip()
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    if not request_id:
        return JsonResponse({'status': 'error', 'message': 'Reprint request ID is required.'}, status=400)
    updated = InvoiceReprintRequest.objects.filter(request_id=request_id).update(
        status='completed' if success else 'failed',
        completed_at=timezone.now(),
        error_message=error,
    )
    if not updated:
        return JsonResponse({'status': 'error', 'message': 'Reprint request was not found.'}, status=404)
    return JsonResponse({'status': 'ok', 'success': success})


@login_required(login_url='/login/')
@csrf_exempt
def adjust_main_stock(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        branch = str(payload.get('branch', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation):
        return JsonResponse({'status': 'error', 'message': 'Product ID and quantity are required.'}, status=400)

    if product_id <= 0 or not product_name or not branch or quantity < 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Product, branch, and a non-negative whole-number stock value are required.'}, status=400)

    for attempt in range(3):
        try:
            with transaction.atomic():
                catalog = ProductCatalog.objects.select_for_update().filter(
                    branch__iexact=branch,
                    product_id=product_id,
                ).first()
                if catalog is None:
                    return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)
                balance, _ = MainStockBalance.objects.select_for_update().get_or_create(product_id=product_id)
                balance.product_name = product_name
                balance.quantity = quantity
                balance.save(update_fields=['product_name', 'quantity', 'updated_at'])
                StockTransfer.objects.create(
                    transfer_id=f'STOCKTAKE_{branch}_{product_id}_{int(time.time() * 1000)}',
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    quantity=quantity,
                    target_quantity=quantity,
                    created_by=request.user.get_username(),
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 2:
                return JsonResponse({'status': 'error', 'message': 'Stock database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.2 * (attempt + 1))

    return JsonResponse({'status': 'ok', 'product_id': product_id, 'branch': branch, 'quantity': str(quantity)})


@login_required(login_url='/login/')
@csrf_exempt
def stocktake_branch_products(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        pin = str(payload.get('pin', '')).strip()
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)
    if not pin or not request.user.check_password(pin):
        return JsonResponse({'status': 'error', 'message': 'Enter the correct PIN for your signed-in account.'}, status=403)

    batch_id = uuid.uuid4().hex
    queued_transfers = []
    skipped_pending = 0
    negative_zeroed = 0
    with transaction.atomic():
        products = list(ProductCatalog.objects.select_for_update().filter(
            branch__iexact=branch,
            branch_confirmed=True,
        ).order_by('product_id'))
        product_ids = [product.product_id for product in products]
        pending_product_ids = set(StockTransfer.objects.filter(
            branch__iexact=branch,
            product_id__in=product_ids,
            target_quantity__isnull=False,
            status__in=['pending', 'processing'],
        ).values_list('product_id', flat=True))

        for product in products:
            if product.product_id in pending_product_ids:
                skipped_pending += 1
                continue
            quantity = product.available_quantity
            if quantity < 0:
                quantity = Decimal('0')
                negative_zeroed += 1
            queued_transfers.append(StockTransfer(
                transfer_id=f'STOCKTAKE_BULK_{batch_id}_{product.product_id}',
                branch=product.branch,
                product_id=product.product_id,
                product_name=product.product_name,
                quantity=quantity,
                target_quantity=quantity,
                created_by=request.user.get_username(),
            ))

        StockTransfer.objects.bulk_create(queued_transfers)

    return JsonResponse({
        'status': 'ok',
        'branch': branch,
        'batch_id': batch_id,
        'queued': len(queued_transfers),
        'skipped_pending': skipped_pending,
        'negative_zeroed': negative_zeroed,
    })


@login_required(login_url='/login/')
@csrf_exempt
def stocktake_branch_batch(request, batch_id):
    if request.method not in {'GET', 'POST'}:
        return JsonResponse({'status': 'error', 'message': 'Use GET or POST method'}, status=405)
    try:
        batch_id = uuid.UUID(batch_id).hex
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Invalid stocktake batch.'}, status=400)

    batch_prefix = f'STOCKTAKE_BULK_{batch_id}_'
    batch_transfers = StockTransfer.objects.filter(
        transfer_id__startswith=batch_prefix,
        target_quantity__isnull=False,
    )
    cancelled_now = 0
    if request.method == 'POST':
        try:
            payload = json.loads(request.body or '{}')
            transfer_id = str(payload.get('transfer_id', '')).strip()
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
        if transfer_id and not transfer_id.startswith(batch_prefix):
            return JsonResponse({'status': 'error', 'message': 'Product does not belong to this stocktake batch.'}, status=400)
        cancel_query = batch_transfers
        if transfer_id:
            cancel_query = cancel_query.filter(transfer_id=transfer_id)
            if not cancel_query.exists():
                return JsonResponse({'status': 'error', 'message': 'Stocktake product not found.'}, status=404)
        cancelled_now = cancel_query.filter(
            status='pending',
            claimed_at__isnull=True,
        ).update(status='cancelled')

    transfers = list(batch_transfers.order_by('product_name', 'product_id'))
    counts = {
        'completed': 0,
        'in_progress': 0,
        'pending': 0,
        'failed': 0,
        'cancelled': 0,
    }
    products = []
    for transfer in transfers:
        if transfer.status == 'pending' and transfer.claimed_at:
            status = 'in_progress'
        elif transfer.status == 'processing':
            status = 'in_progress'
        elif transfer.status in counts:
            status = transfer.status
        else:
            status = 'failed'
        counts[status] += 1
        products.append({
            'transfer_id': transfer.transfer_id,
            'product_id': transfer.product_id,
            'product_name': transfer.product_name,
            'quantity': str(transfer.target_quantity),
            'status': status,
            'can_cancel': status == 'pending',
        })

    return JsonResponse({
        'status': 'ok',
        'batch_id': batch_id,
        'total': len(transfers),
        'counts': counts,
        'products': products,
        'cancelled_now': cancelled_now,
    })


@csrf_exempt
def stock_summary(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = str(request.GET.get('branch', '')).strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)

    selected_date_value = str(request.GET.get('date', '')).strip()
    selected_date = parse_date(selected_date_value) if selected_date_value else timezone.localdate()
    if selected_date is None:
        return JsonResponse({'status': 'error', 'message': 'Use a valid date.'}, status=400)

    catalog = ProductCatalog.objects.filter(branch__iexact=branch, branch_confirmed=True)
    since = parse_datetime(str(request.GET.get('since', '')).strip())
    if since is not None and timezone.is_naive(since):
        since = timezone.make_aware(since, timezone.get_current_timezone())
    selected_day_start = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
    selected_day_end = selected_day_start + timedelta(days=1)
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    snapshot_time = timezone.now()
    full_snapshot = since is None or timezone.localtime(since).date() != today_start.date()

    if full_snapshot:
        products = list(catalog)
    else:
        changed_since = since - timedelta(seconds=3)
        catalog_product_ids = catalog.values_list('product_id', flat=True)
        changed_product_ids = set(catalog.filter(
            updated_at__gte=changed_since,
        ).values_list('product_id', flat=True))
        changed_product_ids.update(MainStockBalance.objects.filter(
            product_id__in=catalog_product_ids,
            updated_at__gte=changed_since,
        ).values_list('product_id', flat=True))
        changed_product_ids.update(StockMovement.objects.filter(
            branch__iexact=branch,
            product_id__in=catalog_product_ids,
            created_at__gte=changed_since,
            created_at__lt=selected_day_end,
        ).values_list('product_id', flat=True))
        changed_product_ids.update(StockTransfer.objects.filter(
            branch__iexact=branch,
            product_id__in=catalog_product_ids,
        ).filter(
            Q(created_at__gte=changed_since)
            | Q(claimed_at__gte=changed_since)
            | Q(completed_at__gte=changed_since),
        ).values_list('product_id', flat=True))
        if not changed_product_ids:
            return JsonResponse({
                'status': 'ok',
                'branch': branch,
                'changed': False,
                'full': False,
                'as_of': snapshot_time.isoformat(),
                'products': [],
            })
        products = list(catalog.filter(product_id__in=changed_product_ids))

    product_ids = [product.product_id for product in products]
    balance_by_product = dict(MainStockBalance.objects.filter(
        product_id__in=product_ids,
    ).values_list('product_id', 'quantity'))

    movement_totals = StockMovement.objects.filter(
        branch__iexact=branch,
        product_id__in=product_ids,
        movement_type__in=['received', 'sold'],
        created_at__gte=selected_day_start,
        created_at__lt=selected_day_end,
    ).values('product_id', 'movement_type').annotate(total=Sum(Cast('quantity', IntegerField())))
    received_by_product = {}
    sold_by_product = {}
    for movement in movement_totals:
        if movement['movement_type'] == 'received':
            received_by_product[movement['product_id']] = movement['total'] or 0
        elif movement['movement_type'] == 'sold':
            sold_by_product[movement['product_id']] = movement['total'] or 0

    sent_by_product = {
        row['product_id']: row['total'] or 0
        for row in StockTransfer.objects.filter(
            product_id__in=product_ids,
            branch__iexact=branch,
            status__in=['pending', 'processing', 'completed'],
            created_at__gte=selected_day_start,
            created_at__lt=selected_day_end,
        ).values('product_id').annotate(total=Sum(Cast('quantity', IntegerField())))
    }
    latest_adjustments = StockMovement.objects.filter(
        branch__iexact=branch,
        product_id=OuterRef('product_id'),
        movement_type__in=['adjusted', 'stocktake', 'maintained'],
    ).order_by('-created_at', '-id')
    latest_adjustment_by_product = {}
    for product_id, quantity in StockMovement.objects.filter(
        branch__iexact=branch,
        product_id__in=product_ids,
        movement_type__in=['adjusted', 'stocktake', 'maintained'],
    ).order_by('product_id', '-created_at', '-id').values_list('product_id', 'quantity'):
        latest_adjustment_by_product.setdefault(product_id, quantity)
    movement_deltas = dict(
        StockMovement.objects.filter(
            branch__iexact=branch,
            product_id__in=product_ids,
        ).annotate(
            latest_adjustment_at=Subquery(latest_adjustments.values('created_at')[:1]),
            latest_adjustment_id=Subquery(latest_adjustments.values('id')[:1]),
        ).filter(
            Q(latest_adjustment_id__isnull=True, movement_type__in=['received', 'sold'])
            | Q(created_at__gt=F('latest_adjustment_at'))
            | Q(created_at=F('latest_adjustment_at'), id__gt=F('latest_adjustment_id')),
        ).values('product_id').annotate(
            total=Sum(Case(
                When(movement_type='received', then=Cast('quantity', IntegerField())),
                When(movement_type='sold', then=-Cast('quantity', IntegerField())),
                default=0,
                output_field=IntegerField(),
            )),
        ).values_list('product_id', 'total')
    )
    summary = []
    for product in products:
        movement_balance = latest_adjustment_by_product.get(product.product_id, 0) + movement_deltas.get(product.product_id, 0)
        has_pos_daily_totals = product.pos_movement_date == selected_date
        summary.append({
            **product_payload(product),
            'main_quantity': str(balance_by_product.get(product.product_id, Decimal('0'))),
            'received_quantity': str(
                product.pos_received_quantity if has_pos_daily_totals and product.pos_received_quantity is not None
                else received_by_product.get(product.product_id, 0)
            ),
            'sent_quantity': str(sent_by_product.get(product.product_id, 0)),
            'sold_quantity': str(
                product.pos_sold_quantity if has_pos_daily_totals and product.pos_sold_quantity is not None
                else sold_by_product.get(product.product_id, 0)
            ),
            'available_quantity': str(product.available_quantity),
            'movement_balance': str(movement_balance),
        })
    return JsonResponse({
        'status': 'ok',
        'branch': branch,
        'changed': True,
        'full': full_snapshot,
        'as_of': snapshot_time.isoformat(),
        'products': summary,
    })


@login_required(login_url='/login/')
def stock_movements(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = str(request.GET.get('branch', '')).strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Select a branch first.'}, status=400)
    selected_date_value = request.GET.get('date', '').strip()
    selected_date = parse_date(selected_date_value) if selected_date_value else timezone.localdate()
    if selected_date is None:
        return JsonResponse({'status': 'error', 'message': 'Use a valid date.'}, status=400)
    timezone_offset_value = request.GET.get('timezone_offset', '0').strip()
    try:
        timezone_offset = int(timezone_offset_value)
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Use a valid timezone offset.'}, status=400)
    if not -840 <= timezone_offset <= 840:
        return JsonResponse({'status': 'error', 'message': 'Timezone offset is out of range.'}, status=400)
    local_day_start = datetime.combine(selected_date, datetime.min.time())
    day_start = timezone.make_aware(local_day_start + timedelta(minutes=timezone_offset))
    day_end = day_start + timedelta(days=1)
    movement_records = list(StockMovement.objects.filter(
        branch__iexact=branch,
        created_at__gte=day_start,
        created_at__lt=day_end,
    ).order_by('-created_at', '-id')[:2000])
    movement_events = []
    for movement in movement_records:
        if movement.source.casefold() == 'pos_stock_snapshot':
            continue
        movement_events.append((movement.created_at, movement.id, {
            'product_id': movement.product_id,
            'product_name': movement.product_name,
            'movement_type': (
                'maintained' if movement.movement_type == 'adjusted' and movement.source == 'pos_stock_snapshot'
                else 'stocktake' if movement.movement_type == 'adjusted' and '(stock take)' in movement.source.casefold()
                else movement.movement_type
            ),
            'quantity': str(movement.quantity),
            'source': {
                'branch_sync': 'POS sales',
                'pos_stock_snapshot': 'POS stock sync',
            }.get(movement.source, movement.source or 'System'),
            'created_at': movement.created_at.isoformat(),
        }))

    movements_by_product = {}
    for movement in movement_records:
        movements_by_product.setdefault(movement.product_id, []).append(movement)
    for product_movements in movements_by_product.values():
        product_movements.sort(key=lambda movement: (movement.created_at, movement.id))
        previous_snapshot = None
        expected_quantity = None
        for movement in product_movements:
            if movement.source.casefold() == 'pos_stock_snapshot':
                if previous_snapshot is not None:
                    quantity_delta = expected_quantity - movement.quantity
                    if quantity_delta != 0:
                        movement_events.append((movement.created_at, movement.id, {
                            'product_id': movement.product_id,
                            'product_name': movement.product_name,
                            'movement_type': 'sold' if quantity_delta > 0 else 'adjusted',
                            'quantity': str(abs(quantity_delta)),
                            'source': 'POS stock sync',
                            'created_at': movement.created_at.isoformat(),
                        }))
                previous_snapshot = movement
                expected_quantity = movement.quantity
            elif previous_snapshot is not None:
                if movement.movement_type in {'adjusted', 'stocktake', 'maintained'}:
                    expected_quantity = movement.quantity
                elif movement.movement_type == 'received':
                    expected_quantity += movement.quantity
                elif movement.movement_type == 'sold':
                    expected_quantity -= movement.quantity

    matched_movement_ids = set()
    transfers = StockTransfer.objects.filter(
        branch__iexact=branch,
        status='completed',
        target_quantity__isnull=True,
        created_at__gte=day_start,
        created_at__lt=day_end,
    ).order_by('-created_at')[:2000]
    for transfer in transfers:
        expected_source = f'{transfer.created_by or "System"} (transfer to {transfer.branch})'
        matching_movement = next((
            movement for movement in movement_records
            if movement.id not in matched_movement_ids
            and movement.product_id == transfer.product_id
            and movement.movement_type == 'received'
            and movement.quantity == transfer.quantity
            and movement.source == expected_source
            and abs((movement.created_at - transfer.created_at).total_seconds()) <= 10
        ), None)
        if matching_movement is not None:
            matched_movement_ids.add(matching_movement.id)
            continue

        movement_events.append((transfer.created_at, transfer.transfer_id, {
            'product_id': transfer.product_id,
            'product_name': transfer.product_name,
            'movement_type': 'received',
            'quantity': str(transfer.quantity),
            'source': 'Stock transfer',
            'created_at': transfer.created_at.isoformat(),
        }))

    movement_events.sort(key=lambda event: event[0], reverse=True)
    return JsonResponse({
        'status': 'ok',
        'movements': [event[2] for event in movement_events[:20]],
    })


@login_required(login_url='/login/')
def product_movement_history_api(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    product_query = str(request.GET.get('product', '')).strip()
    product_id_value = str(request.GET.get('product_id', '')).strip()
    branch = str(request.GET.get('branch', '')).strip()
    from_date_value = request.GET.get('from', '').strip()
    to_date_value = request.GET.get('to', '').strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Select a branch first.'}, status=400)
    if not product_query and not product_id_value:
        return JsonResponse({'status': 'error', 'message': 'Select a product first.'}, status=400)
    try:
        product_id = int(product_id_value) if product_id_value else None
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Use a valid product ID.'}, status=400)
    if product_id is not None and product_id <= 0:
        return JsonResponse({'status': 'error', 'message': 'Use a valid product ID.'}, status=400)
    from_date = parse_date(from_date_value) if from_date_value else None
    to_date = parse_date(to_date_value) if to_date_value else None
    if (from_date_value and from_date is None) or (to_date_value and to_date is None):
        return JsonResponse({'status': 'error', 'message': 'Use valid dates.'}, status=400)
    if from_date and to_date and from_date > to_date:
        return JsonResponse({'status': 'error', 'message': 'The start date must be before the end date.'}, status=400)

    movements = StockMovement.objects.filter(branch__iexact=branch)
    if product_id is not None:
        movements = movements.filter(product_id=product_id)
    elif product_query:
        product_filters = Q(product_name__icontains=product_query)
        if product_query.isdigit():
            product_filters |= Q(product_id=int(product_query))
        movements = movements.filter(product_filters)

    all_movements = list(movements.order_by('created_at', 'id'))
    product_ids = {movement.product_id for movement in all_movements}
    running_balances = {product_id: Decimal('0') for product_id in product_ids}
    movement_rows = []
    for movement in all_movements:
        if movement.movement_type in {'adjusted', 'stocktake', 'maintained'}:
            running_balances[movement.product_id] = movement.quantity
        elif movement.movement_type == 'sold':
            running_balances[movement.product_id] -= movement.quantity
        else:
            running_balances[movement.product_id] += movement.quantity
        if from_date and movement.created_at.date() < from_date:
            continue
        if to_date and movement.created_at.date() > to_date:
            continue
        movement_rows.append({
            'product_id': movement.product_id,
            'product_name': movement.product_name,
            'movement_type': movement.movement_type,
            'quantity': str(movement.quantity),
            'balance': str(running_balances[movement.product_id]),
            'branch': movement.branch or 'Main stock',
            'source': 'Sold' if movement.movement_type == 'sold' else (movement.source or 'System'),
            'created_at': movement.created_at.isoformat(),
        })
    movement_rows.reverse()
    movement_rows = movement_rows[:1000]
    return JsonResponse({
        'status': 'ok',
        'movements': movement_rows,
    })


@login_required(login_url='/login/')
def stock_transfer_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    date_value = request.GET.get('date', '').strip()
    transfers = StockTransfer.objects.order_by('-created_at')
    selected_date = None
    if date_value:
        selected_date = parse_date(date_value)
        if selected_date is None:
            return JsonResponse({'status': 'error', 'message': 'Use a valid transfer date.'}, status=400)
        day_start = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
        day_end = day_start + timedelta(days=1)
        transfers = transfers.filter(created_at__gte=day_start, created_at__lt=day_end)[:200]
    else:
        transfers = transfers[:200]

    transfer_rows = list(transfers)
    return JsonResponse({
        'status': 'ok',
        'date': selected_date.isoformat() if selected_date else None,
        'transfers': [
            {
                'transfer_id': transfer.transfer_id,
                'branch': transfer.branch,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.quantity),
                'status': transfer.status,
                'created_by': transfer.created_by or 'System',
                'created_at': transfer.created_at.isoformat(),
                'completed_at': transfer.completed_at.isoformat() if transfer.completed_at else None,
            }
            for transfer in transfer_rows
        ],
    })


@csrf_exempt
def whatsapp_stock_lookup(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    configured_token = settings.WHATSAPP_STOCK_API_TOKEN
    supplied_token = request.headers.get('X-WhatsApp-Token', '')
    if not configured_token or not hmac.compare_digest(supplied_token, configured_token):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized'}, status=403)

    action = request.GET.get('action', 'transfers').strip().lower()
    if action == 'branches':
        catalog_branches = ProductCatalog.objects.filter(branch_confirmed=True).exclude(branch='')
        transfer_branches = StockTransfer.objects.exclude(branch='')
        branches_by_key = {
            name.casefold(): name
            for name in WHATSAPP_STOCK_BRANCHES
        }
        for name in catalog_branches.values_list('branch', flat=True).distinct():
            if name and name.casefold() != 'main':
                branches_by_key.setdefault(name.casefold(), name)
        for name in transfer_branches.values_list('branch', flat=True).distinct():
            if name and name.casefold() != 'main':
                branches_by_key.setdefault(name.casefold(), name)
        return JsonResponse({
            'status': 'ok',
            'branches': sorted(branches_by_key.values(), key=str.casefold),
        })

    if action != 'transfers':
        return JsonResponse({'status': 'error', 'message': 'Unknown action'}, status=400)

    branch = request.GET.get('branch', '').strip()
    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch is required'}, status=400)

    stock_timezone = ZoneInfo(settings.WHATSAPP_STOCK_TIME_ZONE)
    today = timezone.now().astimezone(stock_timezone).date()
    selected_date_text = request.GET.get('date', '').strip()
    selected_date = parse_date(selected_date_text) if selected_date_text else today
    if selected_date is None:
        return JsonResponse({'status': 'error', 'message': 'Use a valid transfer date in YYYY-MM-DD format.'}, status=400)

    day_start = datetime.combine(selected_date, datetime.min.time(), tzinfo=stock_timezone)
    day_end = day_start + timedelta(days=1)
    transfers = StockTransfer.objects.filter(
        branch__iexact=branch,
        created_at__gte=day_start,
        created_at__lt=day_end,
    ).exclude(transfer_id__startswith='STOCKTAKE_').order_by('created_at', 'product_name')
    return JsonResponse({
        'status': 'ok',
        'branch': branch,
        'date': selected_date.isoformat(),
        'transfers': [
            {
                'transfer_id': transfer.transfer_id,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.quantity),
                'status': transfer.status,
                'sent_at': transfer.created_at.astimezone(stock_timezone).isoformat(),
            }
            for transfer in transfers
        ],
    })


@login_required(login_url='/login/')
def movement_adjustment_history_api(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch = request.GET.get('branch', '').strip()
    query_text = request.GET.get('q', '').strip()
    from_date_text = request.GET.get('from', '').strip()
    to_date_text = request.GET.get('to', '').strip()
    from_date = parse_date(from_date_text) if from_date_text else None
    to_date = parse_date(to_date_text) if to_date_text else None
    if (from_date_text and from_date is None) or (to_date_text and to_date is None):
        return JsonResponse({'status': 'error', 'message': 'Use valid dates.'}, status=400)
    if from_date and to_date and from_date > to_date:
        return JsonResponse({'status': 'error', 'message': 'The start date must be before the end date.'}, status=400)

    adjustments = StockTransfer.objects.filter(
        transfer_id__startswith='STOCKTAKE_',
        target_quantity__isnull=False,
    )
    branches = list(adjustments.exclude(branch='').values_list('branch', flat=True).distinct().order_by('branch'))
    if branch:
        adjustments = adjustments.filter(branch__iexact=branch)
    if query_text:
        search_filter = (
            Q(product_name__icontains=query_text)
            | Q(branch__icontains=query_text)
            | Q(created_by__icontains=query_text)
            | Q(transfer_id__icontains=query_text)
        )
        if query_text.isdigit():
            search_filter |= Q(product_id=int(query_text))
        adjustments = adjustments.filter(search_filter)
    if from_date:
        start = timezone.make_aware(datetime.combine(from_date, datetime.min.time()))
        adjustments = adjustments.filter(created_at__gte=start)
    if to_date:
        end = timezone.make_aware(datetime.combine(to_date + timedelta(days=1), datetime.min.time()))
        adjustments = adjustments.filter(created_at__lt=end)

    total = adjustments.count()
    rows = list(adjustments.order_by('-created_at', '-transfer_id')[:1000])
    return JsonResponse({
        'status': 'ok',
        'count': len(rows),
        'total': total,
        'truncated': total > len(rows),
        'branches': branches,
        'adjustments': [
            {
                'transfer_id': transfer.transfer_id,
                'branch': transfer.branch,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.target_quantity),
                'created_by': transfer.created_by or 'System',
                'created_at': transfer.created_at.isoformat(),
                'completed_at': transfer.completed_at.isoformat() if transfer.completed_at else None,
                'status': 'in_progress' if transfer.status == 'processing' or (transfer.status == 'pending' and transfer.claimed_at) else transfer.status,
            }
            for transfer in rows
        ],
    })


@csrf_exempt
def stock_movement_device_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    movements = StockMovement.objects.order_by('-created_at', '-id')[:2000]
    return JsonResponse({
        'status': 'ok',
        'movements': [
            {
                'id': movement.id,
                'branch': movement.branch,
                'product_id': movement.product_id,
                'product_name': movement.product_name,
                'movement_type': movement.movement_type,
                'quantity': str(movement.quantity),
                'source': movement.source or 'System',
                'created_at': movement.created_at.isoformat(),
            }
            for movement in movements
        ],
    })


@csrf_exempt
def stock_transfer_device_logs(request):
    """Return transfer states for the Windows sync dashboard without claiming work."""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    transfers = StockTransfer.objects.order_by('-created_at')[:200]
    return JsonResponse({
        'status': 'ok',
        'transfers': [
            {
                'id': transfer.transfer_id,
                'branch': transfer.branch,
                'product_id': transfer.product_id,
                'product_name': transfer.product_name,
                'quantity': str(transfer.quantity),
                'status': transfer.status,
                'created_at': transfer.created_at.isoformat(),
                'completed_at': transfer.completed_at.isoformat() if transfer.completed_at else None,
            }
            for transfer in transfers
        ],
    })


@csrf_exempt
def cancellation_device_logs(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    cancellations = DeletionRecord.objects.filter(
        status='processed',
        action='cancel_invoice',
    ).order_by('-confirmation_timestamp', '-timestamp')[:2000]
    return JsonResponse({
        'status': 'ok',
        'cancellations': [
            {
                'id': record.deletion_id,
                'invoice': record.invoice,
                'branch': record.branch,
                'product_id': int(record.product_id) if str(record.product_id or '').isdigit() else None,
                'entry_no': record.entry_no,
                'deleted_rows': record.deleted_rows or 0,
                'status': record.status,
                'cancelled_at': (record.confirmation_timestamp or record.timestamp).isoformat(),
                'message': record.message,
            }
            for record in cancellations
        ],
    })


@csrf_exempt
def record_branch_sales(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)
    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, product name, and quantity are required.'}, status=400)
    if not branch or product_id <= 0 or not product_name or quantity <= 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a positive whole-number quantity are required.'}, status=400)
    movement = StockMovement.objects.create(branch=branch, product_id=product_id, product_name=product_name, movement_type='sold', quantity=quantity, source='branch_sync')
    return JsonResponse({'status': 'ok', 'movement_id': movement.id})


@csrf_exempt
def publish_branch_stock_snapshot(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        products = payload.get('products', [])
        movement_date_value = payload.get('movement_date')
        movement_date = parse_date(str(movement_date_value)) if movement_date_value else timezone.localdate()
        if not isinstance(products, list):
            raise ValueError
        if movement_date is None:
            raise ValueError

        snapshots = []
        for item in products:
            if not isinstance(item, dict):
                raise ValueError
            product_id = int(item.get('product_id'))
            product_name = str(item.get('product_name', '')).strip()
            quantity = Decimal(str(item.get('available_quantity')))
            sold_quantity = abs(Decimal(str(item.get('sold_quantity', 0) or 0))) if 'sold_quantity' in item else None
            received_quantity = Decimal(str(item.get('received_quantity', 0) or 0)) if 'received_quantity' in item else None
            if product_id <= 0 or not product_name or quantity != whole_quantity(quantity):
                raise ValueError
            if sold_quantity is not None and sold_quantity != whole_quantity(sold_quantity):
                raise ValueError
            if received_quantity is not None and received_quantity != whole_quantity(received_quantity):
                raise ValueError
            snapshots.append({
                'product_id': product_id,
                'product_name': product_name,
                'product_code': str(item.get('product_code', '')).strip(),
                'barcode': str(item.get('barcode', '')).strip(),
                'selling_price': Decimal(str(item.get('selling_price', 0) or 0)),
                'available_quantity': quantity,
                'pos_movement_date': movement_date if sold_quantity is not None or received_quantity is not None else None,
                'pos_sold_quantity': sold_quantity,
                'pos_received_quantity': received_quantity,
            })
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and valid product stock snapshots are required.'}, status=400)

    if not branch:
        return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)

    updated = 0
    movements_recorded = 0
    with transaction.atomic():
        for snapshot in snapshots:
            product, created = ProductCatalog.objects.select_for_update().get_or_create(
                branch=branch,
                product_id=snapshot['product_id'],
                defaults={
                    'product_name': snapshot['product_name'],
                    'product_code': snapshot['product_code'],
                    'barcode': snapshot['barcode'],
                    'selling_price': snapshot['selling_price'],
                    'available_quantity': Decimal('0'),
                    'branch_confirmed': True,
                },
            )
            previous_movement_date = product.pos_movement_date
            previous_sold_quantity = abs(product.pos_sold_quantity) if product.pos_sold_quantity is not None else Decimal('0')
            previous_received_quantity = product.pos_received_quantity if product.pos_received_quantity is not None else Decimal('0')
            previous_available_quantity = product.available_quantity
            quantity_changed = product.available_quantity != snapshot['available_quantity']
            sold_delta = Decimal('0')
            received_delta = Decimal('0')
            if snapshot['pos_sold_quantity'] is not None:
                sold_delta = max(snapshot['pos_sold_quantity'] - previous_sold_quantity, Decimal('0'))
                if previous_movement_date != snapshot['pos_movement_date'] or product.pos_sold_quantity is None:
                    sold_delta = snapshot['pos_sold_quantity']
            if snapshot['pos_received_quantity'] is not None:
                received_delta = max(snapshot['pos_received_quantity'] - previous_received_quantity, Decimal('0'))
                if previous_movement_date != snapshot['pos_movement_date'] or product.pos_received_quantity is None:
                    received_delta = snapshot['pos_received_quantity']
            needs_maintenance = quantity_changed and (
                previous_available_quantity + received_delta - sold_delta != snapshot['available_quantity']
            )
            changed_fields = []
            for field in ('product_name', 'product_code', 'barcode', 'selling_price'):
                if getattr(product, field) != snapshot[field]:
                    setattr(product, field, snapshot[field])
                    changed_fields.append(field)
            for field in ('pos_movement_date', 'pos_sold_quantity', 'pos_received_quantity'):
                if snapshot[field] is not None and getattr(product, field) != snapshot[field]:
                    setattr(product, field, snapshot[field])
                    changed_fields.append(field)
            if quantity_changed:
                product.available_quantity = snapshot['available_quantity']
                changed_fields.append('available_quantity')
            if changed_fields:
                product.branch_confirmed = True
                product.updated_at = timezone.now()
                product.save(update_fields=[*changed_fields, 'branch_confirmed', 'updated_at'])
            if changed_fields or created:
                updated += 1
            if sold_delta > 0:
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product.product_id,
                    product_name=product.product_name,
                    movement_type='sold',
                    quantity=sold_delta,
                    source='POS sales',
                )
                movements_recorded += 1
            if received_delta > 0:
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product.product_id,
                    product_name=product.product_name,
                    movement_type='received',
                    quantity=received_delta,
                    source='POS receiving',
                )
                movements_recorded += 1
            if needs_maintenance:
                StockMovement.objects.create(
                    branch=branch,
                    product_id=product.product_id,
                    product_name=product.product_name,
                    movement_type='maintained',
                    quantity=snapshot['available_quantity'],
                    source='pos_stock_snapshot',
                )
                movements_recorded += 1

    return JsonResponse({
        'status': 'ok',
        'branch': branch,
        'updated': updated,
        'movements_recorded': movements_recorded,
    })


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def create_stock_transfer(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        quantity = Decimal(str(payload.get('quantity')))
    except (TypeError, ValueError, InvalidOperation):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, and quantity are required.'}, status=400)

    if not branch or product_id <= 0 or not product_name or quantity <= 0 or quantity != whole_quantity(quantity):
        return JsonResponse({'status': 'error', 'message': 'Branch, product ID, and product name are required; quantity must be a positive whole number.'}, status=400)

    for attempt in range(3):
        try:
            with transaction.atomic():
                balance, _ = MainStockBalance.objects.select_for_update().get_or_create(product_id=product_id)
                balance.product_name = product_name
                balance.quantity += quantity
                balance.save(update_fields=['product_name', 'quantity', 'updated_at'])
                transfer = StockTransfer.objects.create(
                    transfer_id=f'TRANSFER_{branch}_{product_id}_{int(time.time() * 1000)}',
                    branch=branch,
                    product_id=product_id,
                    product_name=product_name,
                    quantity=quantity,
                    created_by=request.user.get_username(),
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 2:
                return JsonResponse({'status': 'error', 'message': 'Stock database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.2 * (attempt + 1))

    return JsonResponse({'status': 'accepted', 'transfer': transfer_payload(transfer)}, status=202)


@login_required(login_url='/login/')
@csrf_exempt
def request_branch_price_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        product_name = str(payload.get('product_name', '')).strip()
        selling_price = Decimal(str(payload.get('selling_price')))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a valid price are required.'}, status=400)

    if not branch or product_id <= 0 or not product_name or selling_price < 0:
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a non-negative price are required.'}, status=400)

    try:
        catalog = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)

    branch_catalogs = ProductCatalog.objects.filter(
        product_id=product_id,
        branch_confirmed=True,
    ).exclude(branch__iexact='MAIN')
    branches = list(branch_catalogs.order_by('branch').values_list('branch', flat=True))
    with transaction.atomic():
        branch_catalogs.update(
            product_name=product_name,
            pending_selling_price=selling_price,
            pending_price_update=True,
            updated_at=timezone.now(),
        )
        ProductCatalog.objects.update_or_create(
            branch='MAIN',
            product_id=product_id,
            defaults={
                'product_name': product_name,
                'product_code': catalog.product_code,
                'barcode': catalog.barcode,
                'selling_price': selling_price,
                'tax_rate': catalog.tax_rate,
                'branch_confirmed': True,
                'pending_selling_price': None,
                'pending_price_update': False,
                'pending_product_creation': False,
            },
        )
    invalidate_product_catalog_cache()
    return JsonResponse({
        'status': 'accepted',
        'branch': catalog.branch,
        'branches': branches,
        'branch_count': len(branches),
        'product_id': catalog.product_id,
        'product_name': catalog.product_name,
        'selling_price': str(selling_price),
    }, status=202)


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def update_product_tax_rate(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        tax_rate = Decimal(str(payload.get('tax_rate')))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product, and a valid tax rate are required.'}, status=400)

    if not branch or branch.casefold() == 'main' or product_id <= 0 or tax_rate < 0 or tax_rate > 100:
        return JsonResponse({'status': 'error', 'message': 'Select a branch product and enter a tax rate from 0 to 100.'}, status=400)

    catalog = ProductCatalog.objects.filter(
        branch__iexact=branch,
        product_id=product_id,
        branch_confirmed=True,
    ).first()
    if catalog is None:
        return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)

    with transaction.atomic():
        branch_count = ProductCatalog.objects.filter(
            product_id=product_id,
            branch_confirmed=True,
        ).exclude(branch__iexact='MAIN').update(tax_rate=tax_rate, updated_at=timezone.now())
        ProductCatalog.objects.filter(branch__iexact='MAIN', product_id=product_id).update(
            tax_rate=tax_rate,
            updated_at=timezone.now(),
        )

    invalidate_product_catalog_cache()
    return JsonResponse({
        'status': 'ok',
        'product_id': product_id,
        'product_name': catalog.product_name,
        'tax_rate': str(tax_rate),
        'branch_count': branch_count,
    })


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def create_branch_product(request):
    global BRANCH_HEARTBEAT_DB_AVAILABLE

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        requested_product_id = int(payload.get('product_id') or 0)
        product_name = str(payload.get('product_name', '')).strip()
        product_code = str(payload.get('product_code', '')).strip()
        barcode = str(payload.get('barcode', '')).strip()
        initial_quantity = Decimal('0')
        selling_price = Decimal(str(payload.get('selling_price', 0) or 0))
        tax_rate = Decimal(str(payload.get('tax_rate', 0) or 0))
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch, product name, and a valid price are required.'}, status=400)

    if requested_product_id and requested_product_id < 12_100_000:
        return JsonResponse({'status': 'error', 'message': 'Product ID must be at least 12100000.'}, status=400)
    if requested_product_id > 2_147_483_647:
        return JsonResponse({'status': 'error', 'message': 'Product ID cannot exceed 2147483647.'}, status=400)
    if branch.casefold() == 'main' or not product_name or len(product_name) > 250:
        return JsonResponse({'status': 'error', 'message': 'A product name is required; Main cannot be selected as a branch.'}, status=400)
    if len(product_code) > 50 or len(barcode) > 100 or initial_quantity < 0 or initial_quantity != whole_quantity(initial_quantity) or selling_price < 0 or tax_rate < 0 or tax_rate > 100:
        return JsonResponse({'status': 'error', 'message': 'Product code, barcode, quantity, and price are invalid.'}, status=400)

    branch_targets = set(ProductCatalog.objects.exclude(branch__iexact='MAIN').exclude(branch='').values_list('branch', flat=True))
    if BRANCH_HEARTBEAT_DB_AVAILABLE:
        try:
            branch_targets.update(BranchHeartbeat.objects.values_list('branch', flat=True))
        except ProgrammingError:
            BRANCH_HEARTBEAT_DB_AVAILABLE = False
    if not BRANCH_HEARTBEAT_DB_AVAILABLE:
        branch_targets.update(item['name'] for item in CONNECTED_BRANCHES.values())
    if branch:
        branch_targets.add(branch)

    branches_by_key = {}
    for target in branch_targets:
        target = str(target).strip()
        if target and target.casefold() != 'main':
            branches_by_key.setdefault(target.casefold(), target)
    branches = sorted(branches_by_key.values(), key=str.casefold)
    if not branches:
        return JsonResponse({'status': 'error', 'message': 'No branches are registered to receive this product.'}, status=400)

    with transaction.atomic():
        if requested_product_id:
            product_id = requested_product_id
        else:
            maximum_id = ProductCatalog.objects.select_for_update().filter(product_id__gte=12_100_000).aggregate(max_id=Max('product_id'))['max_id']
            product_id = (maximum_id or 12_099_999) + 1
        if ProductCatalog.objects.filter(product_id=product_id).exists():
            return JsonResponse({'status': 'error', 'message': f'Product ID {product_id} is already queued. Enter another unique Product ID.'}, status=409)
        product_code = str(product_id + 1)
        products = ProductCatalog.objects.bulk_create([
            ProductCatalog(
                branch=target,
                product_id=product_id,
                product_name=product_name,
                product_code=product_code,
                barcode=barcode,
                pending_stock_quantity=initial_quantity,
                selling_price=selling_price,
                tax_rate=tax_rate,
                branch_confirmed=False,
                pending_product_creation=True,
            )
            for target in branches
        ])
        product = products[0]
    invalidate_product_catalog_cache()

    return JsonResponse({
        'status': 'accepted',
        'branch': product.branch,
        'product_id': product.product_id,
        'product_name': product.product_name,
        'product_code': product.product_code,
        'barcode': product.barcode,
        'initial_quantity': str(product.pending_stock_quantity),
        'selling_price': str(product.selling_price),
        'tax_rate': str(product.tax_rate),
        'branches': branches,
    }, status=202)


@csrf_exempt
@retry_on_database_lock
def complete_branch_product_creation(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
        actual_product_id = int(payload.get('actual_product_id') or product_id)
        success = bool(payload.get('success'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    try:
        product = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The pending product was not found.'}, status=404)

    if success:
        with transaction.atomic():
            product.product_id = actual_product_id
            product.product_code = str(actual_product_id)
            product.branch_confirmed = True
            product.pending_product_creation = False
            product.save(update_fields=['product_id', 'product_code', 'branch_confirmed', 'pending_product_creation', 'updated_at'])
            ProductCatalog.objects.update_or_create(
                branch='MAIN',
                product_id=actual_product_id,
                defaults={
                    'product_name': product.product_name,
                    'product_code': product.product_code,
                    'barcode': product.barcode,
                    'selling_price': product.selling_price,
                    'tax_rate': product.tax_rate,
                    'branch_confirmed': True,
                    'pending_product_creation': False,
                },
            )
            MainStockBalance.objects.get_or_create(
                product_id=actual_product_id,
                defaults={'product_name': product.product_name, 'quantity': 0},
            )
        invalidate_product_catalog_cache()
    return JsonResponse({'status': 'ok', 'branch': product.branch, 'product_id': product.product_id, 'success': success})


@login_required(login_url='/login/')
@csrf_exempt
@retry_on_database_lock
def delete_branch_product(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id') or 0)
        password = str(payload.get('password', ''))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    if not password or not request.user.check_password(password):
        return JsonResponse({'status': 'error', 'message': 'Enter your account password to confirm product deletion.'}, status=403)
    if not branch or branch.casefold() == 'main' or product_id <= 0:
        return JsonResponse({'status': 'error', 'message': 'Select a branch product to delete.'}, status=400)

    with transaction.atomic():
        branch_products = list(ProductCatalog.objects.select_for_update().filter(
            product_id=product_id,
            branch_confirmed=True,
        ).exclude(branch__iexact='MAIN').order_by('branch'))
        product = next(
            (item for item in branch_products if item.branch.casefold() == branch.casefold()),
            None,
        )
        if product is None:
            return JsonResponse({'status': 'error', 'message': 'The product was not found for this branch.'}, status=404)
        if ProductCatalog.objects.filter(
            product_id=product_id,
            pending_product_creation=True,
        ).exclude(branch__iexact='MAIN').exists():
            return JsonResponse({'status': 'error', 'message': 'Wait for product creation to finish before deleting it.'}, status=409)
        if ProductDeletionRequest.objects.filter(
            product_id=product_id,
            status='pending',
        ).exists():
            return JsonResponse({'status': 'error', 'message': 'A deletion for this product is already queued.'}, status=409)

        deletion_requests = []
        for branch_product in branch_products:
            if StockTransfer.objects.filter(
                branch__iexact=branch_product.branch,
                product_id=product_id,
                status__in=['pending', 'processing'],
            ).exists():
                return JsonResponse({
                    'status': 'error',
                    'message': f"Wait for this product's stock transfers to finish at {branch_product.branch} before deleting it.",
                }, status=409)

            deletion_requests.append(ProductDeletionRequest(
                branch=branch_product.branch,
                product_id=branch_product.product_id,
                product_name=branch_product.product_name,
                requested_by=request.user.get_username(),
            ))
        ProductDeletionRequest.objects.bulk_create(deletion_requests)

    invalidate_product_catalog_cache()
    return JsonResponse({
        'status': 'accepted',
        'request_id': next(
            item.pk for item in deletion_requests
            if item.branch.casefold() == product.branch.casefold()
        ),
        'product_id': product.product_id,
        'product_name': product.product_name,
        'branches': [item.branch for item in deletion_requests],
        'deletions': [
            {'request_id': item.pk, 'branch': item.branch}
            for item in deletion_requests
        ],
    }, status=202)


@csrf_exempt
@retry_on_database_lock
def complete_branch_product_deletion(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        request_id = int(payload.get('request_id'))
        success = bool(payload.get('success'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and deletion request are required.'}, status=400)

    with transaction.atomic():
        deletion = ProductDeletionRequest.objects.select_for_update().filter(
            pk=request_id,
            branch__iexact=branch,
        ).first()
        if deletion is None:
            return JsonResponse({'status': 'error', 'message': 'The deletion request was not found.'}, status=404)
        if deletion.status != 'pending':
            return JsonResponse({'status': 'ok', 'deletion_status': deletion.status})

        deletion.status = 'completed' if success else 'failed'
        deletion.error = '' if success else str(payload.get('error', 'Product deletion failed.'))[:2000]
        deletion.save(update_fields=['status', 'error', 'updated_at'])
        if success:
            ProductCatalog.objects.filter(
                branch__iexact=deletion.branch,
                product_id=deletion.product_id,
            ).delete()

    invalidate_product_catalog_cache()
    return JsonResponse({'status': 'ok', 'request_id': deletion.pk, 'deletion_status': deletion.status})


@csrf_exempt
@retry_on_database_lock
def complete_branch_price_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        branch = str(payload.get('branch', '')).strip()
        product_id = int(payload.get('product_id'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    if not branch or product_id <= 0:
        return JsonResponse({'status': 'error', 'message': 'Branch and product are required.'}, status=400)

    try:
        catalog = ProductCatalog.objects.get(branch__iexact=branch, product_id=product_id)
    except ProductCatalog.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'The product was not found for the selected branch.'}, status=404)

    if payload.get('success'):
        if catalog.pending_price_update and catalog.pending_selling_price is not None:
            catalog.selling_price = catalog.pending_selling_price
            catalog.pending_selling_price = None
            catalog.pending_price_update = False
            catalog.save(update_fields=['selling_price', 'pending_selling_price', 'pending_price_update', 'updated_at'])

    return JsonResponse({'status': 'ok', 'product_id': product_id, 'branch': branch, 'success': bool(payload.get('success'))})


@login_required(login_url='/login/')
def product_catalog(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    query = str(request.GET.get('q', '')).strip()
    branch = str(request.GET.get('branch', '')).strip()
    cache_version = cache.get(PRODUCT_CACHE_VERSION_KEY, 0)
    request_key = hashlib.sha256(f'{branch.casefold()}:{query.casefold()}'.encode()).hexdigest()
    cache_key = f'lora:products:{cache_version}:{request_key}'
    cached_products = cache.get(cache_key)
    if cached_products is not None:
        return JsonResponse({'status': 'ok', 'products': cached_products})

    products = ProductCatalog.objects.filter(branch__iexact=branch or 'MAIN')
    if branch and branch.casefold() != 'main':
        products = products.filter(branch_confirmed=True).exclude(
            product_id__in=ProductDeletionRequest.objects.filter(
                branch__iexact=branch,
                status='pending',
            ).values('product_id'),
        )
    if query:
        query_value = query.strip()
        product_filters = (
            Q(product_name__icontains=query_value)
            | Q(product_code__icontains=query_value)
            | Q(barcode__icontains=query_value)
        )
        try:
            product_id_filter = int(query_value)
        except ValueError:
            product_id_filter = None
        if product_id_filter is not None:
            product_filters |= Q(product_id=product_id_filter)
        products = products.filter(product_filters)[:30]
    else:
        products = products[:3000]
    product_rows = [product_payload(product) for product in products]
    cache.set(cache_key, product_rows, PRODUCT_CACHE_SECONDS)
    return JsonResponse({'status': 'ok', 'products': product_rows})


@login_required(login_url='/login/')
def deleted_products(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    deletions = ProductDeletionRequest.objects.filter(status='completed').order_by('-updated_at')[:500]
    return JsonResponse({
        'status': 'ok',
        'products': [
            {
                'branch': item.branch,
                'product_id': item.product_id,
                'product_name': item.product_name,
                'deleted_by': item.requested_by,
                'deleted_at': item.updated_at.isoformat(),
            }
            for item in deletions
        ],
    })


@csrf_exempt
def product_catalog_sync_disabled(request):
    return JsonResponse(
        {'status': 'disabled', 'message': 'Branch product catalog sync is no longer supported.'},
        status=410,
    )


@csrf_exempt
def product_sync_inbox(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    products = ProductCatalog.objects.exclude(branch__iexact='MAIN').filter(branch_confirmed=True)
    since = parse_datetime(str(request.GET.get('since', '')).strip())
    full_snapshot = since is None
    if not full_snapshot:
        if timezone.is_naive(since):
            since = timezone.make_aware(since, timezone.get_current_timezone())
        products = products.filter(updated_at__gte=since - timedelta(seconds=3))
    changed_products = [product_payload(product) for product in products]
    return JsonResponse({
        'status': 'ok',
        'changed': bool(changed_products),
        'full': full_snapshot,
        'products': changed_products,
    })


@csrf_exempt
def shared_product_catalog(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    since = parse_datetime(str(request.GET.get('since', '')).strip())
    full_snapshot = since is None
    products = ProductCatalog.objects.filter(branch_confirmed=True).order_by(
        Case(When(branch__iexact='MAIN', then=0), default=1, output_field=IntegerField()),
        'product_id',
        '-updated_at',
        'branch',
    )
    if not full_snapshot:
        if timezone.is_naive(since):
            since = timezone.make_aware(since, timezone.get_current_timezone())
        changed_product_ids = ProductCatalog.objects.filter(
            branch_confirmed=True,
            updated_at__gte=since - timedelta(seconds=3),
        ).values_list('product_id', flat=True).distinct()
        products = products.filter(product_id__in=changed_product_ids)

    shared_products = {}
    for product in products.iterator():
        shared_products.setdefault(product.product_id, {
            'product_id': product.product_id,
            'product_name': product.product_name,
            'product_code': product.product_code,
            'barcode': product.barcode,
            'selling_price': str(product.selling_price),
            'tax_rate': str(product.tax_rate),
        })

    return JsonResponse({
        'status': 'ok',
        'changed': bool(shared_products),
        'full': full_snapshot,
        'products': list(shared_products.values()),
    })


@csrf_exempt
def publish_product_catalog(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
        products = payload.get('products', [])
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    if not isinstance(products, list):
        return JsonResponse({'status': 'error', 'message': 'Products must be a list.'}, status=400)

    catalog_rows = []
    for item in products:
        try:
            product_id = int(item.get('product_id'))
            available_quantity = Decimal(str(item.get('available_quantity', 0) or 0))
            selling_price = Decimal(str(item.get('selling_price', 0) or 0))
            raw_tax_rate = item.get('tax_rate')
            if raw_tax_rate is None or str(raw_tax_rate).strip() == '':
                existing_product = ProductCatalog.objects.filter(product_id=product_id).order_by('-updated_at', '-id').first()
                tax_rate = existing_product.tax_rate if existing_product and existing_product.tax_rate is not None else Decimal('0')
            else:
                tax_rate = Decimal(str(raw_tax_rate))
        except (TypeError, ValueError, InvalidOperation):
            continue
        product_name = str(item.get('product_name', '')).strip()
        if product_id <= 0 or not product_name:
            continue
        catalog_rows.append(ProductCatalog(
            branch=str(item.get('branch', 'MAIN')).strip() or 'MAIN',
            product_id=product_id,
            product_name=product_name,
            product_code=str(item.get('product_code', '')).strip(),
            barcode=str(item.get('barcode', '')).strip(),
            available_quantity=available_quantity,
            selling_price=selling_price,
            branch_confirmed=True,
            tax_rate=tax_rate,
            pending_price_update=False,
            pending_selling_price=None,
            pending_product_creation=False,
            updated_at=timezone.now(),
        ))

    for attempt in range(5):
        try:
            with transaction.atomic():
                ProductCatalog.objects.bulk_create(
                    catalog_rows,
                    update_conflicts=True,
                    update_fields=[
                        'product_name', 'product_code', 'barcode', 'available_quantity',
                        'selling_price', 'branch_confirmed', 'tax_rate', 'pending_price_update',
                        'pending_selling_price', 'pending_product_creation', 'updated_at',
                    ],
                    unique_fields=['branch', 'product_id'],
                    batch_size=500,
                )
            break
        except OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 4:
                return JsonResponse({'status': 'error', 'message': 'Product catalog database is busy. Please retry in a moment.'}, status=503)
            time.sleep(0.25 * (attempt + 1))

    return JsonResponse({'status': 'ok', 'updated': len(catalog_rows)})


@csrf_exempt
@retry_on_database_lock
def complete_sales_report(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    request_id = str(payload.get('report_id', '')).strip()
    if not request_id:
        return JsonResponse({'status': 'error', 'message': 'Missing report_id'}, status=400)

    try:
        report = SalesReportRequest.objects.get(request_id=request_id)
    except SalesReportRequest.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Report request not found'}, status=404)

    report.status = 'printed' if payload.get('success') else 'failed'
    report.row_count = int(payload.get('row_count') or 0)
    report.error_message = str(payload.get('error') or '')
    report.completed_at = timezone.now()
    report.save(update_fields=['status', 'row_count', 'error_message', 'completed_at'])
    return JsonResponse({'status': 'ok', 'report': report_payload(report)})


@csrf_exempt
@retry_on_database_lock
def complete_stock_transfer(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    transfer_id = str(payload.get('transfer_id', '')).strip()
    branch = str(payload.get('branch', '')).strip()
    if not transfer_id or not branch:
        return JsonResponse({'status': 'error', 'message': 'Transfer ID and branch are required.'}, status=400)

    try:
        transfer = StockTransfer.objects.get(transfer_id=transfer_id)
    except StockTransfer.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Transfer not found.'}, status=404)

    if not transfer.branch.casefold() == branch.casefold():
        return JsonResponse({'status': 'error', 'message': 'Transfer branch does not match.'}, status=403)

    if payload.get('success'):
        if transfer.status != 'completed':
            with transaction.atomic():
                catalog, created = get_or_create_branch_catalog(
                    transfer.branch,
                    transfer.product_id,
                    {
                        'product_name': transfer.product_name,
                        'available_quantity': transfer.quantity,
                    },
                )
                is_stock_take = transfer.target_quantity is not None
                if is_stock_take:
                    catalog.product_name = transfer.product_name or catalog.product_name
                    catalog.available_quantity = transfer.target_quantity
                    movement_source = f'{transfer.created_by or "System"} (stock take)'
                    legacy_movement_exists = StockMovement.objects.filter(
                        branch__iexact=transfer.branch,
                        product_id=transfer.product_id,
                        movement_type='stocktake',
                        quantity=transfer.target_quantity,
                        source=movement_source,
                        created_at__gte=transfer.created_at - timedelta(seconds=10),
                        created_at__lte=transfer.created_at + timedelta(seconds=10),
                    ).exists()
                    if not legacy_movement_exists:
                        StockMovement.objects.create(
                            branch=transfer.branch,
                            product_id=transfer.product_id,
                            product_name=transfer.product_name,
                            movement_type='stocktake',
                            quantity=transfer.target_quantity,
                            source=movement_source,
                        )
                elif not created and not transfer.branch_stock_applied:
                    catalog.product_name = transfer.product_name or catalog.product_name
                    catalog.available_quantity += transfer.quantity
                elif created:
                    catalog.product_name = transfer.product_name or catalog.product_name
                    catalog.available_quantity = transfer.quantity
                applied_stock = created or not transfer.branch_stock_applied
                if not is_stock_take and applied_stock:
                    movement_date = timezone.localdate()
                    if catalog.pos_movement_date != movement_date:
                        catalog.pos_movement_date = movement_date
                        catalog.pos_sold_quantity = Decimal('0')
                        catalog.pos_received_quantity = Decimal('0')
                    catalog.pos_received_quantity = (catalog.pos_received_quantity or Decimal('0')) + transfer.quantity
                    StockMovement.objects.create(
                        branch=transfer.branch,
                        product_id=transfer.product_id,
                        product_name=transfer.product_name,
                        movement_type='received',
                        quantity=transfer.quantity,
                        source='Web stock transfer',
                    )
                if is_stock_take or applied_stock:
                    catalog.pending_stock_adjustment = True
                    catalog.pending_stock_quantity = catalog.available_quantity
                    update_fields = ['product_name', 'available_quantity', 'pending_stock_adjustment', 'pending_stock_quantity', 'updated_at']
                    if not is_stock_take and applied_stock:
                        update_fields.extend(['pos_movement_date', 'pos_sold_quantity', 'pos_received_quantity'])
                    catalog.save(update_fields=update_fields)
                transfer.status = 'completed'
                transfer.completed_at = transfer.completed_at or timezone.now()
                transfer.retry_count = 0
                transfer.retry_after = None
                transfer.branch_stock_applied = True
                transfer.save(update_fields=[
                    'status', 'completed_at', 'claimed_at', 'retry_count',
                    'retry_after', 'branch_stock_applied',
                ])
    elif transfer.status != 'completed':
        transfer.retry_count += 1
        if transfer.retry_count >= 6:
            transfer.status = 'failed'
            transfer.claimed_at = None
            transfer.retry_after = None
            transfer.completed_at = transfer.completed_at or timezone.now()
            transfer.save(update_fields=['status', 'claimed_at', 'retry_count', 'retry_after', 'completed_at'])
        else:
            retry_minutes = min(60, 5 * (2 ** min(transfer.retry_count - 1, 4)))
            transfer.status = 'pending'
            transfer.claimed_at = None
            transfer.retry_after = timezone.now() + timedelta(minutes=retry_minutes)
            transfer.save(update_fields=['status', 'claimed_at', 'retry_count', 'retry_after'])

    return JsonResponse({'status': 'ok', 'transfer': transfer_payload(transfer)})


@csrf_exempt
def branch_status(request):
    """Register a branch heartbeat or list branches currently online."""
    now = time.time()

    if request.method == 'POST':
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

        branch = str(payload.get('branch', '')).strip()
        device_role = str(payload.get('device_role', '')).strip()
        if not branch or device_role.casefold() != 'branch pc':
            return JsonResponse({'status': 'error', 'message': 'A saved branch name and Branch PC device role are required.'}, status=400)

        global BRANCH_HEARTBEAT_DB_AVAILABLE
        if BRANCH_HEARTBEAT_DB_AVAILABLE:
            try:
                BranchHeartbeat.objects.update_or_create(
                    branch__iexact=branch,
                    defaults={
                        'branch': branch,
                        'last_seen': timezone.now(),
                        'device_role': 'Branch PC',
                    },
                )
            except ProgrammingError:
                BRANCH_HEARTBEAT_DB_AVAILABLE = False
        if not BRANCH_HEARTBEAT_DB_AVAILABLE:
            CONNECTED_BRANCHES[branch.lower()] = {
                'name': branch,
                'last_seen': now,
                'device_role': 'Branch PC',
            }
        return JsonResponse({'status': 'online', 'branch': branch})

    if request.method == 'GET':
        if BRANCH_HEARTBEAT_DB_AVAILABLE:
            try:
                stale_before = timezone.now() - timedelta(seconds=BRANCH_ONLINE_SECONDS)
                branches = [
                    {
                        'name': branch.branch,
                        'last_seen': branch.last_seen.timestamp(),
                        'device_role': branch.device_role,
                        'online': True,
                    }
                    for branch in BranchHeartbeat.objects.filter(last_seen__gt=stale_before)
                ]
            except ProgrammingError:
                BRANCH_HEARTBEAT_DB_AVAILABLE = False
        if not BRANCH_HEARTBEAT_DB_AVAILABLE:
            branches = [
                {**branch, 'online': True}
                for branch in CONNECTED_BRANCHES.values()
                if now - branch['last_seen'] <= BRANCH_ONLINE_SECONDS
            ]
        return JsonResponse({'status': 'ok', 'branches': branches, 'count': len(branches), 'online_count': len(branches)})

    return JsonResponse({'status': 'error', 'message': 'Use GET or POST method'}, status=405)


@csrf_exempt
def favicon(request):
    """Favicon endpoint - returns empty response"""
    return HttpResponse('', content_type='image/x-icon')


@csrf_exempt
def health_check(request):
    return JsonResponse({
        'status': 'ok',
        'service': 'Lora API Gateway',
        'message': 'Django gateway is running.'
    })


def cleanup_stale_pending_transfers(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(hours=1)
    stale_transfers = StockTransfer.objects.filter(
        status='pending',
    ).filter(
        Q(retry_count__gte=6, retry_after__lt=cutoff)
        | Q(retry_count__gte=6, retry_after__isnull=True, created_at__lt=cutoff)
        | Q(created_at__lt=now - timedelta(days=1), retry_after__isnull=True)
    )
    if not stale_transfers.exists():
        return 0

    stale_transfers.update(
        status='failed',
        claimed_at=None,
        retry_after=None,
        completed_at=timezone.now(),
    )
    return stale_transfers.count()


def claim_next_stock_transfer(branch_name=''):
    now = timezone.now()
    cleanup_stale_pending_transfers(now)
    stale_before = now - timedelta(minutes=2)
    StockTransfer.objects.filter(
        status='processing',
        created_at__lt=stale_before,
    ).update(status='pending', claimed_at=None)
    transfer_query = StockTransfer.objects.filter(status='pending').filter(
        Q(retry_after__isnull=True) | Q(retry_after__lte=now),
    ).order_by('created_at')
    if branch_name:
        transfer_query = transfer_query.filter(branch__iexact=branch_name)
    with transaction.atomic():
        transfer = transfer_query.select_for_update().first()
        if transfer and (transfer.claimed_at is None or transfer.claimed_at < stale_before):
            transfer.claimed_at = now
            transfer.save(update_fields=['claimed_at'])
        else:
            transfer = None
    return transfer


@csrf_exempt
def pending_stock_transfers(request):
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Use GET method'}, status=405)

    branch_name = request.GET.get('branch', '').strip()
    if not branch_name:
        return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)

    transfer = claim_next_stock_transfer(branch_name)
    transfers = [transfer] if transfer else []
    return JsonResponse({
        'status': 'ok',
        'branch_filter': branch_name,
        'pending_transfers': [transfer_payload(item) for item in transfers],
    })


@csrf_exempt
def branch_sync(request):
    """
    TRIGGER SYSTEM FOR BRANCH DELETIONS

    GET: Branch polls for pending deletions
    POST: Branch notifies of deletion completion (from branch cancel)
    """
    cleanup_queues()

    if request.method == 'GET':
        branch_name = request.GET.get('branch', '').strip()
        queue_due_sales_reports(branch_name)

        if not branch_name:
            ProductCatalog.objects.filter(
                Q(pending_price_update=True)
                | Q(pending_product_creation=True)
                | Q(pending_stock_adjustment=True)
            ).update(
                pending_price_update=False,
                pending_selling_price=None,
                pending_product_creation=False,
                pending_stock_quantity=0,
                pending_stock_adjustment=False,
                updated_at=timezone.now(),
            )
            pending_price_updates = []
            pending_product_creations = []
        else:
            pending_price_updates = list(ProductCatalog.objects.filter(
                branch__iexact=branch_name,
                pending_price_update=True,
                pending_selling_price__isnull=False,
            ).values('branch', 'product_id', 'product_name', 'pending_selling_price'))
            pending_product_creations = list(ProductCatalog.objects.filter(
                branch__iexact=branch_name,
                pending_product_creation=True,
            ).values('branch', 'product_id', 'product_name', 'product_code', 'barcode', 'pending_stock_quantity', 'selling_price', 'tax_rate'))

        pending_query = DeletionRecord.objects.filter(status='pending')
        if branch_name:
            pending_query = pending_query.filter(branch__iexact=branch_name)
        with transaction.atomic():
            pending_records = list(pending_query.select_for_update())
            for record in pending_records:
                record.status = 'processing'
                record.save(update_fields=['status'])
        pending = [record_payload(item) for item in pending_records]

        report_query = SalesReportRequest.objects.filter(status='pending')
        if branch_name:
            report_query = report_query.filter(branch__iexact=branch_name)
        with transaction.atomic():
            reports = list(report_query.select_for_update())
            for report in reports:
                report.status = 'processing'
                report.save(update_fields=['status'])

        transfer = claim_next_stock_transfer(branch_name)
        transfers = [transfer] if transfer else []

        pending_product_deletions = list(ProductDeletionRequest.objects.filter(
            branch__iexact=branch_name,
            status='pending',
        ).values('id', 'branch', 'product_id', 'product_name'))
        with transaction.atomic():
            reprint = InvoiceReprintRequest.objects.select_for_update().filter(
                branch__iexact=branch_name,
                status='pending',
            ).order_by('requested_at').first()
            reprints = []
            if reprint:
                reprint.status = 'processing'
                reprint.save(update_fields=['status'])
                reprints.append(reprint)

        return JsonResponse({
            'status': 'ok',
            'service': 'branch_sync_trigger',
            'branch_filter': branch_name,
            'pending_deletions': pending,
            'count': len(pending),
            'pending_reports': [report_payload(item) for item in reports],
            'pending_transfers': [transfer_payload(item) for item in transfers],
            'pending_price_updates': [
                {
                    'branch': item['branch'],
                    'product_id': item['product_id'],
                    'product_name': item['product_name'],
                    'selling_price': str(item['pending_selling_price']),
                }
                for item in pending_price_updates
            ],
            'pending_product_creations': [
                {
                    'branch': item['branch'],
                    'product_id': item['product_id'],
                    'product_name': item['product_name'],
                    'product_code': item['product_code'],
                    'barcode': item['barcode'],
                    'initial_quantity': str(item['pending_stock_quantity'] or 0),
                    'selling_price': str(item['selling_price']),
                    'tax_rate': str(item['tax_rate']),
                }
                for item in pending_product_creations
            ],
            'pending_product_deletions': [
                {
                    'request_id': item['id'],
                    'branch': item['branch'],
                    'product_id': item['product_id'],
                    'product_name': item['product_name'],
                }
                for item in pending_product_deletions
            ],
            'pending_invoice_reprints': [
                {'request_id': item.request_id, 'branch': item.branch, 'invoice': item.invoice}
                for item in reprints
            ],
            'message': f'Found {len(pending)} deletion(s) to process'
        })

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = payload.get('invoice')
    product_id = payload.get('product_id')
    entry_no = payload.get('entry_no')
    branch = payload.get('branch', 'unknown')
    deleted = payload.get('deleted', False)

    if not invoice or not deleted:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required fields: invoice, deleted=true'
        }, status=400)

    deletion_id = f"{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=branch,
        invoice=str(invoice),
        product_id=str(product_id) if product_id is not None else None,
        entry_no=str(entry_no) if entry_no is not None else '',
        action=payload.get('action', 'delete'),
        status='pending',
        source='branch_cancel',
        message=f'Branch {branch} cancelled invoice {invoice}',
    )

    return JsonResponse({
        'status': 'accepted',
        'message': f'Deletion request queued for invoice {invoice}',
        'deletion_id': deletion_record.deletion_id,
        'queue_size': DeletionRecord.objects.filter(status='pending').count()
    }, status=202)


@csrf_exempt
def main_sync(request):
    """
    MAIN SYNC ENDPOINT - Main database sends deletions to branches via trigger

    POST: Main requests branch to delete (triggered by cancellation)
    GET: Main checks deletion status
    """
    cleanup_queues()

    if request.method == 'GET':
        queue_due_sales_reports()
        pending = [record_payload(item) for item in DeletionRecord.objects.filter(status__in=['pending', 'processing'])]
        processed = [record_payload(item) for item in DeletionRecord.objects.filter(status='processed').order_by('-confirmation_timestamp')[:10]]
        failed = [record_payload(item) for item in DeletionRecord.objects.filter(status='failed').order_by('-confirmation_timestamp')[:10]]
        pending_reports = [report_payload(item) for item in SalesReportRequest.objects.filter(status__in=['scheduled', 'pending', 'processing']).order_by('-requested_at')[:20]]
        completed_reports = [report_payload(item) for item in SalesReportRequest.objects.filter(status__in=['printed', 'failed']).order_by('-completed_at')[:20]]
        queue = [
            {
                'type': 'cancellation',
                'id': item['id'],
                'branch': item['branch'],
                'label': f"Cancellation {item['invoice']}",
                'detail': 'Whole invoice',
                'status': item['status'],
                'timestamp': item['timestamp'],
                'completed_at': item['confirmation_timestamp'],
            }
            for item in pending + processed + failed
        ] + [
            {
                'type': 'sales_report',
                'id': item['id'],
                'branch': item['branch'],
                'label': 'Sales report',
                'detail': item['report_date'],
                'status': item['status'],
                'timestamp': item['requested_at'],
                'scheduled_at': item['scheduled_at'],
                'completed_at': item['completed_at'],
            }
            for item in pending_reports + completed_reports
        ]
        queue.sort(key=lambda item: item['timestamp'], reverse=True)

        response = JsonResponse({
            'status': 'ok',
            'service': 'main_sync_trigger',
            'pending_deletions': pending,
            'pending_count': DeletionRecord.objects.filter(status__in=['pending', 'processing']).count(),
            'recently_processed': processed,
            'processed_count': DeletionRecord.objects.filter(status='processed').count(),
            'queue': queue[:40],
        })
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    invoice = payload.get('invoice')
    product_id = payload.get('product_id')
    entry_no = payload.get('entry_no')
    branch = payload.get('branch')
    deleted_from_main = payload.get('deleted_from_main', False)

    if not invoice:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required field: invoice'
        }, status=400)

    deletion_id = f"MAIN_{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}"
    deletion_record = DeletionRecord.objects.create(
        deletion_id=deletion_id,
        branch=str(branch or ''),
        invoice=str(invoice),
        product_id=str(product_id) if product_id is not None else None,
        entry_no=str(entry_no) if entry_no is not None else '',
        status='pending',
        source='main_cancellation',
        deleted_from_main=deleted_from_main,
        message=f'Main cancelled invoice {invoice} - DELETE FROM ALL BRANCHES',
    )

    return JsonResponse({
        'status': 'triggered',
        'message': f'Deletion trigger issued for invoice {invoice} on branch {branch}',
        'deletion_id': deletion_record.deletion_id,
        'queue_size': DeletionRecord.objects.filter(status='pending').count()
    }, status=202)


@csrf_exempt
@retry_on_database_lock
def confirm_deletion(request):
    """
    CONFIRMATION ENDPOINT - Branch confirms deletion was successful

    POST: Branch sends confirmation that deletion succeeded
    """
    cleanup_queues()

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Use POST method'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    deletion_id = payload.get('deletion_id')
    deleted_rows_raw = payload.get('deleted_rows', 0)
    try:
        deleted_rows = int(deleted_rows_raw)
    except (TypeError, ValueError):
        deleted_rows = 0
    branch = payload.get('branch')
    success_flag = payload.get('success')
    success = bool(success_flag) if success_flag is not None else deleted_rows > 0
    deleted_by = str(
        payload.get('deleted_by') or payload.get('username') or payload.get('user_number') or ''
    ).strip()
    approval_pending = bool(payload.get('approval_pending', False))
    receipt_products = payload.get('receipt_products') or []
    receipt_total = payload.get('receipt_total')

    if not deletion_id:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing deletion_id'
        }, status=400)

    with transaction.atomic():
        try:
            deletion_record = DeletionRecord.objects.select_for_update().get(deletion_id=deletion_id)
        except DeletionRecord.DoesNotExist:
            return JsonResponse({
                'status': 'error',
                'message': f'Deletion ID {deletion_id} not found in queue'
            }, status=404)

        deletion_record.status = (
            'processing' if approval_pending else
            'processed' if success and deleted_rows > 0 else
            'failed'
        )
        deletion_record.deleted_rows = deleted_rows
        if deleted_by and not deletion_record.deleted_by:
            deletion_record.deleted_by = deleted_by
        deletion_record.confirmed_branch = branch
        deletion_record.confirmation_timestamp = None if approval_pending else timezone.now()
        if deletion_record.status == 'failed':
            deletion_record.message = f'{deletion_record.message} Branch matched no invoice rows.'
        deletion_record.receipt_products = json.dumps(receipt_products)
        try:
            deletion_record.receipt_total = Decimal(str(receipt_total or '0'))
        except InvalidOperation:
            deletion_record.receipt_total = Decimal('0')
        deletion_record.save(update_fields=[
            'status', 'deleted_rows', 'deleted_by', 'confirmed_branch', 'confirmation_timestamp', 'receipt_products', 'receipt_total'
        ])

    return JsonResponse({
        'status': 'awaiting_approval' if approval_pending else deletion_record.status,
        'message': (
            f'Cancellation submitted for POS approval: {deleted_rows} row(s)'
            if approval_pending else f'Deletion {deletion_record.status}: {deleted_rows} row(s) deleted'
        ),
        'deletion_id': deletion_id,
        'branch': branch
    })
