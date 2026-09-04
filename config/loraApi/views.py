import json
import time
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

"""
================================================================================
LORA POS RETURNS - DELETION TRIGGER SYSTEM
================================================================================

This API acts as a trigger mediator for invoice deletions across branch and main 
databases. It ensures consistent deletion of the SAME invoice across all systems.

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

# Deletion requests that need to be processed by branches
DELETION_QUEUE = []

# Track processed deletions
PROCESSED_DELETIONS = []

# Branches are considered online while they continue sending heartbeats.
CONNECTED_BRANCHES = {}
BRANCH_ONLINE_SECONDS = 20


def cleanup_queues():
    """Remove old items from queues"""
    global DELETION_QUEUE, PROCESSED_DELETIONS
    current_time = time.time()
    
    # Keep pending deletions (status='pending') indefinitely, keep 'processing' for 60s
    DELETION_QUEUE = [
        item for item in DELETION_QUEUE
        if item.get('status') == 'pending' or (current_time - item.get('timestamp', current_time)) < 60
    ]
    
    # Keep confirmed items long enough for the dashboard to show recent invoices.
    PROCESSED_DELETIONS = [
        item for item in PROCESSED_DELETIONS
        if (current_time - item.get('confirmation_timestamp', item.get('timestamp', current_time))) < 86400
    ]


@csrf_exempt
def index(request):
    """Browser dashboard for managing branch sale cancellations."""
    cleanup_queues()
    return render(request, 'loraApi/dashboard.html', {
        'pending_count': len([item for item in DELETION_QUEUE if item.get('status') == 'pending']),
        'processed_count': len(PROCESSED_DELETIONS),
    })


@csrf_exempt
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

    deletion_record = {
        'id': f"WEB_{branch}_{invoice}_{int(time.time()*1000)}",
        'branch': branch,
        'invoice': invoice,
        'product_id': None,
        'entry_no': '',
        'action': 'cancel_invoice',
        'status': 'pending',
        'timestamp': time.time(),
        'source': 'web_dashboard',
        'message': f'Web cancellation requested for invoice {invoice} at {branch}'
    }
    DELETION_QUEUE.append(deletion_record)

    return JsonResponse({'status': 'accepted', 'message': f'Cancellation queued for invoice {invoice}.', 'deletion_id': deletion_record['id']}, status=202)


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
        if not branch:
            return JsonResponse({'status': 'error', 'message': 'Branch is required.'}, status=400)

        CONNECTED_BRANCHES[branch.lower()] = {
            'name': branch,
            'last_seen': now,
            'device_role': payload.get('device_role', 'Branch PC'),
        }
        return JsonResponse({'status': 'online', 'branch': branch})

    if request.method == 'GET':
        online = [
            branch for branch in CONNECTED_BRANCHES.values()
            if now - branch['last_seen'] <= BRANCH_ONLINE_SECONDS
        ]
        online.sort(key=lambda branch: branch['name'].lower())
        return JsonResponse({'status': 'ok', 'branches': online, 'count': len(online)})

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


@csrf_exempt
def branch_sync(request):
    """
    TRIGGER SYSTEM FOR BRANCH DELETIONS
    
    GET: Branch polls for pending deletions
    POST: Branch notifies of deletion completion (from branch cancel)
    """
    cleanup_queues()
    
    if request.method == 'GET':
        # Get query parameters to filter by branch
        branch_name = request.GET.get('branch', '').strip()
        
        if not branch_name:
            # Return all pending deletions if no branch specified
            pending = [item for item in DELETION_QUEUE if item.get('status') == 'pending']
        else:
            # Return only pending deletions for this specific branch
            pending = [
                item for item in DELETION_QUEUE 
                if item.get('status') == 'pending' and item.get('branch', '').lower() == branch_name.lower()
            ]
        
        return JsonResponse({
            'status': 'ok',
            'service': 'branch_sync_trigger',
            'branch_filter': branch_name,
            'pending_deletions': pending,
            'count': len(pending),
            'message': f'Found {len(pending)} deletion(s) to process'
        })

    # POST: Receive deletion notification from branch
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    
    # Branch sends: invoice, product_id, entry_no, branch, deleted=true
    # when it cancels an invoice locally
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
    
    # Create deletion record
    deletion_record = {
        'id': f"{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}",
        'branch': branch,
        'invoice': invoice,
        'product_id': product_id,
        'entry_no': entry_no,
        'action': payload.get('action', 'delete'),
        'status': 'pending',  # pending -> processing -> processed
        'timestamp': time.time(),
        'source': 'branch_cancel',
        'message': f'Branch {branch} cancelled invoice {invoice}'
    }
    
    DELETION_QUEUE.append(deletion_record)
    
    return JsonResponse({
        'status': 'accepted',
        'message': f'Deletion request queued for invoice {invoice}',
        'deletion_id': deletion_record.get('id'),
        'queue_size': len(DELETION_QUEUE)
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
        # Main checks pending and recently processed deletions
        pending = [item for item in DELETION_QUEUE if item.get('status') in ['pending', 'processing']]
        processed = PROCESSED_DELETIONS[-10:]  # Last 10 processed
        
        return JsonResponse({
            'status': 'ok',
            'service': 'main_sync_trigger',
            'pending_deletions': pending,
            'pending_count': len(pending),
            'recently_processed': processed,
            'processed_count': len(PROCESSED_DELETIONS)
        })

    # POST: Main sends deletion request to all branches
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    
    # Main sends: invoice, product_id, entry_no, branch(es)
    # This is the authoritative deletion command
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
    
    # Create deletion trigger from main
    deletion_record = {
        'id': f"MAIN_{branch}_{invoice}_{product_id}_{entry_no}_{int(time.time()*1000)}",
        'branch': branch,
        'invoice': invoice,
        'product_id': product_id,
        'entry_no': entry_no,
        'status': 'pending',
        'timestamp': time.time(),
        'source': 'main_cancellation',
        'deleted_from_main': deleted_from_main,
        'message': f'Main cancelled invoice {invoice} - DELETE FROM ALL BRANCHES'
    }
    
    DELETION_QUEUE.append(deletion_record)
    
    return JsonResponse({
        'status': 'triggered',
        'message': f'Deletion trigger issued for invoice {invoice} on branch {branch}',
        'deletion_id': deletion_record.get('id'),
        'queue_size': len(DELETION_QUEUE)
    }, status=202)


@csrf_exempt
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
    deleted_rows = payload.get('deleted_rows', 0)
    branch = payload.get('branch')
    success = payload.get('success', False)
    
    if not deletion_id:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing deletion_id'
        }, status=400)
    
    # Find and mark the deletion as processed
    for item in DELETION_QUEUE:
        if item.get('id') == deletion_id:
            item['status'] = 'processed'
            item['deleted_rows'] = deleted_rows
            item['confirmed_branch'] = branch
            item['confirmation_timestamp'] = time.time()
            
            # Move to processed list
            PROCESSED_DELETIONS.append(item)
            DELETION_QUEUE.remove(item)
            
            return JsonResponse({
                'status': 'confirmed',
                'message': f'Deletion confirmed: {deleted_rows} row(s) deleted',
                'deletion_id': deletion_id,
                'branch': branch
            })
    
    return JsonResponse({
        'status': 'error',
        'message': f'Deletion ID {deletion_id} not found in queue'
    }, status=404)
