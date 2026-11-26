from datetime import datetime, timedelta, timezone as datetime_timezone
import re
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from loraApi.state_store import load_state
from django.utils import timezone
from loraApi.models import BranchHeartbeat, DeletedProductSighting, DeletionRecord, InvoiceReprintRequest, MainStockBalance, ProductCatalog, ProductDeletionRequest, ProductRename, ProductRenameBranch, RenameIgnoredBranch, SalesReportRequest, SalesReportSchedule, StockMovement, StockTransfer
import importlib
import json


from django.core.cache import cache

# Migration 0043 removes these test branches from renames and deletions on the live
# site. The tests use BranchA as a normal branch, so clear that once for the test run.
TEST_BRANCH_MIGRATION = importlib.import_module('loraApi.migrations.0043_remove_test_branches_from_deletions')
MINI_MARKET_RETURNS_MIGRATION = importlib.import_module('loraApi.migrations.0046_mini_market_returns_to_sale_day')


def setUpModule():
	RenameIgnoredBranch.objects.filter(branch__in=TEST_BRANCH_MIGRATION.TEST_BRANCHES, removed_by='setup').delete()
class ProductDeletionTests(TestCase):
	def setUp(self):
		self.password = 'ProductDeletePass4182!'
		admin = get_user_model().objects.create_superuser(username='product-delete-admin', password=self.password)
		self.client.force_login(admin)
		ProductCatalog.objects.create(branch='BranchA', product_id=3001, product_name='Branch Product')
		ProductCatalog.objects.create(branch='BranchB', product_id=3001, product_name='Branch Product')
		ProductCatalog.objects.create(branch='MAIN', product_id=3001, product_name='Branch Product')

	def test_health_route_is_available_without_api_prefix(self):
		response = self.client.get('/health/')
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['status'], 'ok')
		self.assertEqual(response.json()['service'], 'Lora API Gateway')

	def test_deletion_requires_the_current_users_password(self):
		for password in ['', 'not-the-password']:
			with self.subTest(password='missing' if not password else 'incorrect'):
				response = self.client.post(
					'/api/products/delete/',
					data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': password}),
					content_type='application/json',
				)
				self.assertEqual(response.status_code, 403)
		self.assertFalse(ProductDeletionRequest.objects.exists())

	def test_deletion_is_queued_for_all_branches_with_the_selected_product_id(self):
		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['branch'], 'BranchA')
		deletions = response.json()['deletions']
		self.assertEqual(response.json()['branches'], ['BranchA', 'BranchB'])
		request_ids = {item['branch']: item['request_id'] for item in deletions}
		self.assertEqual(self.client.get('/api/products/?branch=BranchA').json()['products'], [])
		self.assertEqual(self.client.get('/api/products/?branch=BranchB').json()['products'], [])
		self.assertEqual(self.client.get('/api/products/?branch=MAIN').json()['products'], [])
		self.assertTrue(ProductCatalog.objects.filter(branch='MAIN', product_id=3001).exists())
		self.assertEqual(self.client.get('/api/branch-sync/?branch=BranchA').json()['pending_product_deletions'], [{
			'request_id': request_ids['BranchA'],
			'branch': 'BranchA',
			'product_id': 3001,
			'product_name': 'Branch Product',
		}])
		self.assertEqual(self.client.get('/api/branch-sync/?branch=BranchB').json()['pending_product_deletions'], [{
			'request_id': request_ids['BranchB'],
			'branch': 'BranchB',
			'product_id': 3001,
			'product_name': 'Branch Product',
		}])

		for target_branch in ('BranchA', 'BranchB'):
			with self.subTest(branch=target_branch):
				complete = self.client.post(
					'/api/products/delete/complete/',
					data=json.dumps({
						'request_id': request_ids[target_branch],
						'branch': target_branch,
						'success': True,
					}),
					content_type='application/json',
				)
				self.assertEqual(complete.status_code, 200)
				self.assertEqual(
					ProductDeletionRequest.objects.get(pk=request_ids[target_branch]).status,
					'completed',
				)

		self.assertFalse(ProductCatalog.objects.filter(branch='BranchA', product_id=3001).exists())
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchB', product_id=3001).exists())
		self.assertTrue(ProductCatalog.objects.filter(branch='MAIN', product_id=3001).exists())

	def test_restore_queues_product_reactivation_for_all_branches(self):
		BranchHeartbeat.objects.create(
			branch='Offline Branch',
			last_seen=timezone.now() - timedelta(days=1),
		)
		deletions = []
		for branch in ('BranchA', 'BranchB'):
			deletion = ProductDeletionRequest.objects.create(
				branch=branch,
				product_id=3001,
				product_name='Branch Product',
				status='completed',
			)
			deletions.append(deletion)
			complete = self.client.post(
				'/api/products/delete/complete/',
				data=json.dumps({
					'request_id': deletion.pk,
					'branch': branch,
					'success': True,
				}),
				content_type='application/json',
			)
			self.assertEqual(complete.status_code, 200)

		response = self.client.post(
			'/api/products/restore/',
			data=json.dumps({'product_id': 3001}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['branches'], ['BranchA', 'BranchB', 'Offline Branch'])
		self.assertEqual(
			set(ProductDeletionRequest.objects.values_list('status', flat=True)),
			{'restored'},
		)
		self.assertEqual(
			{product['product_id'] for product in self.client.get('/api/products/shared/').json()['products']},
			{3001},
		)
		self.assertEqual(
			{product['restore_status'] for product in self.client.get('/api/products/deleted/').json()['products']},
			{'queued'},
		)

		for branch in ('BranchA', 'BranchB', 'Offline Branch'):
			with self.subTest(branch=branch):
				sync = self.client.get(f'/api/branch-sync/?branch={branch}').json()
				self.assertEqual(len(sync['pending_product_creations']), 1)
				self.assertEqual(sync['pending_product_creations'][0]['branch'], branch)
				self.assertEqual(sync['pending_product_creations'][0]['product_id'], 3001)
				self.assertEqual(sync['pending_product_creations'][0]['product_name'], 'Branch Product')
				self.assertEqual(sync['pending_product_deletions'], [])
				self.assertEqual(self.client.get(f'/api/products/?branch={branch}').json()['products'], [])

				complete = self.client.post(
					'/api/products/create/complete/',
					data=json.dumps({
						'branch': branch,
						'product_id': 3001,
						'actual_product_id': 3001,
						'success': True,
					}),
					content_type='application/json',
				)
				self.assertEqual(complete.status_code, 200)
				self.assertTrue(ProductCatalog.objects.get(branch=branch, product_id=3001).branch_confirmed)
				self.assertEqual(len(self.client.get(f'/api/products/?branch={branch}').json()['products']), 1)
				if branch == 'BranchA':
					self.assertEqual(
						{product['restore_status'] for product in self.client.get('/api/products/deleted/').json()['products']},
						{'queued'},
					)

		self.assertEqual(
			{product['restore_status'] for product in self.client.get('/api/products/deleted/').json()['products']},
			{'restored'},
		)

	def test_deletion_targets_product_id_not_product_name(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=3002, product_name='Branch Product')
		ProductCatalog.objects.create(branch='BranchB', product_id=3002, product_name='Branch Product')

		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['product_id'], 3001)
		self.assertTrue(ProductCatalog.objects.filter(branch='BranchA', product_id=3002).exists())
		self.assertTrue(ProductCatalog.objects.filter(branch='BranchB', product_id=3002).exists())
		self.assertEqual(
			set(ProductDeletionRequest.objects.values_list('product_id', flat=True)),
			{3001},
		)

	def test_reconnected_branch_snapshot_does_not_restore_a_deleted_product(self):
		deletion = ProductDeletionRequest.objects.create(
			branch='BranchB', product_id=3001, product_name='Branch Product',
		)
		completed = self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({
				'request_id': deletion.pk,
				'branch': 'BranchB',
				'success': True,
			}),
			content_type='application/json',
		)
		self.assertEqual(completed.status_code, 200)

		response = self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({
				'branch': 'BranchB',
				'products': [{
					'product_id': 3001,
					'product_name': 'Branch Product',
					'available_quantity': '0',
				}],
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['updated'], 0)
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchB', product_id=3001).exists())
		self.assertEqual(ProductDeletionRequest.objects.get(pk=deletion.pk).status, 'completed')

	def test_deletion_includes_registered_and_historical_offline_branches(self):
		BranchHeartbeat.objects.create(branch='Offline Heartbeat Branch', last_seen=timezone.now() - timedelta(days=30))
		StockMovement.objects.create(
			branch='Legacy Stock Branch',
			product_id=900,
			product_name='Unrelated product',
			movement_type='received',
			quantity=1,
		)
		DeletionRecord.objects.create(
			deletion_id='legacy-branch-cancel',
			branch='Legacy Cancellation Branch',
			invoice='INV-OLD',
			status='processed',
		)

		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(
			set(response.json()['branches']),
			{'BranchA', 'BranchB', 'Offline Heartbeat Branch', 'Legacy Stock Branch', 'Legacy Cancellation Branch'},
		)
		self.assertEqual(
			set(ProductDeletionRequest.objects.values_list('branch', flat=True)),
			set(response.json()['branches']),
		)

	def test_deletion_queues_during_creation_and_waits_for_final_product_id(self):
		pending_product = ProductCatalog.objects.get(branch='BranchB', product_id=3001)
		pending_product.branch_confirmed = False
		pending_product.pending_product_creation = True
		pending_product.save(update_fields=['branch_confirmed', 'pending_product_creation', 'updated_at'])

		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		branch_poll = self.client.get('/api/branch-sync/?branch=BranchB').json()
		self.assertEqual(branch_poll['pending_product_creations'][0]['product_id'], 3001)
		self.assertEqual(branch_poll['pending_product_deletions'], [])

		creation_complete = self.client.post(
			'/api/products/create/complete/',
			data=json.dumps({
				'branch': 'BranchB',
				'product_id': 3001,
				'actual_product_id': 9001,
				'success': True,
			}),
			content_type='application/json',
		)

		self.assertEqual(creation_complete.status_code, 200)
		deletion = ProductDeletionRequest.objects.get(branch='BranchB')
		self.assertEqual(deletion.product_id, 9001)
		next_poll = self.client.get('/api/branch-sync/?branch=BranchB').json()
		self.assertEqual(next_poll['pending_product_deletions'][0]['product_id'], 9001)

	def test_deletion_is_rejected_if_any_branch_has_an_active_transfer(self):
		StockTransfer.objects.create(
			transfer_id='branch-b-transfer',
			branch='BranchB',
			product_id=3001,
			product_name='Branch Product',
			quantity=1,
			status='processing',
		)

		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 3001, 'password': self.password}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 409)
		self.assertFalse(ProductDeletionRequest.objects.exists())
		self.assertEqual(len(self.client.get('/api/products/?branch=BranchA').json()['products']), 1)
		self.assertEqual(len(self.client.get('/api/products/?branch=BranchB').json()['products']), 1)

	def test_deleted_products_page_and_api_show_completed_deletions(self):
		completed = ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=3001, product_name='Branch Product',
			requested_by='product-delete-admin', status='completed',
		)
		ProductDeletionRequest.objects.create(
			branch='BranchB', product_id=3001, product_name='Pending product', status='pending',
		)

		page = self.client.get('/products/deleted/')
		response = self.client.get('/api/products/deleted/')

		self.assertEqual(page.status_code, 200)
		self.assertContains(page, 'Deleted products')
		self.assertEqual(len(response.json()['products']), 2)
		self.assertEqual(
			{item['status'] for item in response.json()['products']},
			{'completed', 'pending'},
		)
		self.assertIn(
			'Branch Product',
			{item['product_name'] for item in response.json()['products']},
		)
		self.assertContains(page, 'Sync status')
		self.assertContains(page, 'Movements')
		self.assertContains(page, 'Restore everywhere')

	def test_deleted_product_is_hidden_globally_while_its_movements_remain(self):
		deletion = ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=3001, product_name='Branch Product',
		)
		StockMovement.objects.create(
			branch='BranchB', product_id=3001, product_name='Branch Product',
			movement_type='received', quantity=4, source='historical receipt',
		)

		completed = self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({
				'request_id': deletion.pk,
				'branch': 'BranchA',
				'success': True,
			}),
			content_type='application/json',
		)
		self.assertEqual(completed.status_code, 200)

		self.assertEqual(self.client.get('/api/products/?branch=MAIN').json()['products'], [])
		self.assertEqual(self.client.get('/api/products/?branch=BranchB').json()['products'], [])
		self.assertNotIn(
			3001,
			[item['product_id'] for item in self.client.get('/api/products/shared/').json()['products']],
		)
		self.assertNotIn(
			3001,
			[item['product_id'] for item in self.client.get('/api/products/inbox/').json()['products']],
		)
		self.assertNotIn(
			3001,
			[item['product_id'] for item in self.client.get('/api/stock/product-matrix/').json()['products']],
		)
		self.assertNotIn(
			3001,
			[item['product_id'] for item in self.client.get('/api/stock/summary/?branch=BranchB').json()['products']],
		)

		movements = self.client.get('/api/stock/movements/history/', {
			'branch': 'BranchB',
			'product_id': 3001,
		})
		self.assertEqual(movements.status_code, 200)
		self.assertEqual(len(movements.json()['movements']), 1)
		self.assertEqual(movements.json()['movements'][0]['source'], 'historical receipt')

		published = self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchC',
				'product_id': 3001,
				'product_name': 'Branch Product',
				'available_quantity': 7,
			}]}),
			content_type='application/json',
		)
		self.assertEqual(published.status_code, 200)
		self.assertEqual(published.json()['updated'], 0)
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchC', product_id=3001).exists())

	def test_stock_page_shows_delete_password_tax_and_history_controls(self):
		response = self.client.get('/stock/')

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Delete from all branches')
		self.assertContains(response, 'Your account password')
		self.assertContains(response, 'Save tax')
		self.assertContains(response, '/products/deleted/')
		self.assertContains(response, 'offline or reconnecting; no transfer was sent.')
		self.assertContains(response, 'Adjust all product movements')
		self.assertContains(response, 'movement-adjustment-confirm-dialog')
		self.assertContains(response, 'Your login PIN / account password')
		self.assertContains(response, '/stock/movement-adjustments/')
		self.assertContains(response, 'stocktake-cancel-all')
		self.assertContains(response, 'stocktake-product-list')


class ProductTaxRateTests(TestCase):
	def setUp(self):
		admin = get_user_model().objects.create_superuser(username='product-tax-admin', password='ProductTaxPass4182!')
		self.client.force_login(admin)

	def test_tax_rate_updates_confirmed_products_and_shared_pos_feed(self):
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			ProductCatalog.objects.create(branch=branch, product_id=401, product_name='Tax Product', tax_rate='5.00')
		ProductCatalog.objects.create(
			branch='BranchPending', product_id=401, product_name='Tax Product',
			tax_rate='5.00', branch_confirmed=False,
		)
		response = self.client.post(
			'/api/products/tax-rate/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 401, 'tax_rate': '7.50'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			self.assertEqual(str(ProductCatalog.objects.get(branch=branch, product_id=401).tax_rate), '7.50')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchPending', product_id=401).tax_rate), '5.00')
		shared = next(item for item in self.client.get('/api/products/shared/').json()['products'] if item['product_id'] == 401)
		self.assertEqual(shared['tax_rate'], '7.50')

	def _save_cost(self, cost_price, branch='BranchA', product_id=501):
		return self.client.post(
			'/api/products/cost-price/',
			data=json.dumps({'branch': branch, 'product_id': product_id, 'cost_price': cost_price}),
			content_type='application/json',
		)

	def _snapshot_cost(self, branch, cost_price):
		return self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': branch, 'products': [{
				'product_id': 501, 'product_name': 'Cost Product', 'available_quantity': 5, 'cost_price': cost_price,
			}]}),
			content_type='application/json',
		)

	def _shared_cost(self):
		shared = next(item for item in self.client.get('/api/products/shared/').json()['products'] if item['product_id'] == 501)
		return shared.get('cost_price')

	def test_cost_price_is_saved_for_every_branch_and_sent_to_pos(self):
		for branch, cost in [('BranchA', '2.00'), ('BranchB', '3.00'), ('MAIN', '2.50')]:
			ProductCatalog.objects.create(branch=branch, product_id=501, product_name='Cost Product', available_quantity=5, cost_price=cost)
		ProductCatalog.objects.create(branch='BranchPending', product_id=501, product_name='Cost Product', branch_confirmed=False)
		self.assertIsNone(self._shared_cost())

		response = self._save_cost('4.255')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['cost_price'], '4.26')
		self.assertEqual(response.json()['branch_count'], 2)
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			product = ProductCatalog.objects.get(branch=branch, product_id=501)
			self.assertEqual((str(product.cost_price), str(product.pending_cost_price)), ('4.26', '4.26'))
		self.assertIsNone(ProductCatalog.objects.get(branch='BranchPending', product_id=501).cost_price)
		self.assertEqual(self._shared_cost(), '4.26')

	def test_branch_with_old_cost_does_not_undo_the_web_cost(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=501, product_name='Cost Product', available_quantity=5, cost_price='2.00')
		self._save_cost('4.00')
		self._snapshot_cost('BranchA', '2.00')
		product = ProductCatalog.objects.get(branch='BranchA', product_id=501)
		self.assertEqual((str(product.cost_price), str(product.pending_cost_price)), ('4.00', '4.00'))

		self._snapshot_cost('BranchA', '4')
		product = ProductCatalog.objects.get(branch='BranchA', product_id=501)
		self.assertEqual(str(product.cost_price), '4.00')
		self.assertIsNone(product.pending_cost_price)

	def test_pos_reports_its_cost_when_none_is_waiting(self):
		self._snapshot_cost('BranchA', '1.5')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=501).cost_price), '1.50')
		self._snapshot_cost('BranchA', '1.75')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=501).cost_price), '1.75')
		self._snapshot_cost('BranchA', 'bad')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=501).cost_price), '1.75')
		self.assertIsNone(self._shared_cost())

	def test_cost_price_rejects_negative_and_main(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=501, product_name='Cost Product')
		ProductCatalog.objects.create(branch='MAIN', product_id=501, product_name='Cost Product')
		self.assertEqual(self._save_cost('-1').status_code, 400)
		self.assertEqual(self._save_cost('abc').status_code, 400)
		self.assertEqual(self._save_cost('NaN').status_code, 400)
		self.assertEqual(self._save_cost('1', branch='MAIN').status_code, 400)
		self.assertIsNone(ProductCatalog.objects.get(branch='BranchA', product_id=501).cost_price)

	def test_adjust_all_costs_uses_the_chosen_branch(self):
		rows = [
			('BranchA', 601, '2.00', True), ('BranchA', 602, '5.00', True), ('BranchA', 603, None, True),
			('BranchB', 601, '3.00', True), ('BranchB', 602, '5.00', True), ('BranchB', 603, '9.00', True),
			('BranchC', 601, None, True), ('MAIN', 601, '1.00', True), ('BranchPending', 601, '7.00', False),
		]
		for branch, product_id, cost, confirmed in rows:
			ProductCatalog.objects.create(branch=branch, product_id=product_id, product_name=f'P{product_id}', cost_price=cost, branch_confirmed=confirmed)

		response = self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'brancha'}), content_type='application/json')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['products'], 2)
		self.assertEqual(response.json()['updated_rows'], 3)
		self.assertEqual(response.json()['branch_count'], 2)
		cost = lambda branch, product_id: ProductCatalog.objects.get(branch=branch, product_id=product_id)
		for branch in ['BranchB', 'BranchC', 'MAIN']:
			self.assertEqual((str(cost(branch, 601).cost_price), str(cost(branch, 601).pending_cost_price)), ('2.00', '2.00'))
		self.assertIsNone(cost('BranchB', 602).pending_cost_price)
		self.assertEqual(str(cost('BranchB', 603).cost_price), '9.00')
		self.assertEqual(str(cost('BranchPending', 601).cost_price), '7.00')
		self.assertIsNone(cost('BranchA', 601).pending_cost_price)
		shared = {item['product_id']: item.get('cost_price') for item in self.client.get('/api/products/shared/').json()['products']}
		self.assertEqual(shared[601], '2.00')

		again = self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'BranchA'}), content_type='application/json')
		self.assertEqual(again.json()['updated_rows'], 0)

	def test_adjust_all_costs_can_skip_zero_costs(self):
		for branch, product_id, cost in [('BranchA', 621, '0.00'), ('BranchA', 622, '4.00'), ('BranchB', 621, '3.00'), ('BranchB', 622, '1.00')]:
			ProductCatalog.objects.create(branch=branch, product_id=product_id, product_name=f'P{product_id}', cost_price=cost)
		response = self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'BranchA', 'skip_zero': True}), content_type='application/json')
		self.assertEqual(response.json()['products'], 1)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchB', product_id=621).cost_price), '3.00')
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchB', product_id=622).cost_price), '4.00')
		ProductCatalog.objects.filter(branch='BranchA', product_id=622).update(cost_price=0)
		only_zero = self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'BranchA', 'skip_zero': True}), content_type='application/json')
		self.assertEqual(only_zero.status_code, 400)

	def test_cost_progress_shows_each_branch(self):
		from django.utils import timezone
		BranchHeartbeat.objects.create(branch='BranchA', last_seen=timezone.now())
		BranchHeartbeat.objects.create(branch='BranchB', last_seen=timezone.now() - timedelta(hours=2))
		for branch, product_id, cost in [('BranchA', 631, '2.00'), ('BranchA', 632, '2.00'), ('BranchB', 631, '1.00'), ('BranchC', 631, None), ('BranchD', 631, '3.00')]:
			ProductCatalog.objects.create(branch=branch, product_id=product_id, product_name=f'P{product_id}', cost_price=cost)
		ProductCatalog.objects.filter(branch__in=['BranchA', 'BranchB', 'BranchC'], product_id=631).update(cost_price='5.00', pending_cost_price='5.00')

		data = self.client.get('/api/products/cost-price/progress/').json()

		states = {item['branch']: (item['state'], item['waiting']) for item in data['branches']}
		self.assertEqual(states['BranchA'], ('updating', 1))
		self.assertEqual(states['BranchB'], ('needs_update', 1))
		self.assertEqual(states['BranchC'], ('needs_update', 1))
		self.assertEqual(states['BranchD'], ('done', 0))
		self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchA', 'products': [{'product_id': 631, 'product_name': 'P631', 'available_quantity': 0, 'cost_price': '5'}]}),
			content_type='application/json',
		)
		states = {item['branch']: item['state'] for item in self.client.get('/api/products/cost-price/progress/').json()['branches']}
		self.assertEqual(states['BranchA'], 'done')

	def test_cancel_cost_updates_puts_the_old_cost_back(self):
		for branch, cost in [('BranchA', '2.00'), ('BranchB', '3.00'), ('BranchC', None)]:
			ProductCatalog.objects.create(branch=branch, product_id=641, product_name='P641', cost_price=cost)
		self._save_cost('6.00', product_id=641)
		self._save_cost('7.00', product_id=641)
		cost = lambda branch: ProductCatalog.objects.get(branch=branch, product_id=641)
		self.assertEqual(str(cost('BranchB').previous_cost_price), '3.00')

		one = self.client.post('/api/products/cost-price/cancel/', data=json.dumps({'branch': 'branchb'}), content_type='application/json')
		self.assertEqual(one.json()['cancelled'], 1)
		self.assertEqual((str(cost('BranchB').cost_price), cost('BranchB').pending_cost_price), ('3.00', None))
		self.assertEqual(str(cost('BranchA').pending_cost_price), '7.00')

		self.client.post('/api/products/cost-price/cancel/', data=json.dumps({}), content_type='application/json')
		self.assertEqual((str(cost('BranchA').cost_price), cost('BranchA').pending_cost_price), ('2.00', None))
		self.assertIsNone(cost('BranchC').cost_price)
		shared = next(item for item in self.client.get('/api/products/shared/').json()['products'] if item['product_id'] == 641)
		self.assertNotIn('cost_price', shared)

	def test_adjust_all_costs_needs_reported_costs(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=611, product_name='P611')
		response = self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'BranchA'}), content_type='application/json')
		self.assertEqual(response.status_code, 400)
		self.assertEqual(self.client.post('/api/products/cost-price/copy-branch/', data=json.dumps({'branch': 'MAIN'}), content_type='application/json').status_code, 400)

	def test_tax_rate_rejects_values_over_one_hundred(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=402, product_name='Tax Product')
		response = self.client.post(
			'/api/products/tax-rate/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 402, 'tax_rate': '100.01'}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 400)
		self.assertEqual(str(ProductCatalog.objects.get(product_id=402).tax_rate), '0.00')


class ProductRenameTests(TestCase):
	def setUp(self):
		admin = get_user_model().objects.create_superuser(username='product-rename-admin', password='ProductRenamePass4182!')
		self.client.force_login(admin)
		for branch in ['BranchA', 'BranchB', 'MAIN']:
			ProductCatalog.objects.create(branch=branch, product_id=501, product_name='Old Name', available_quantity=4)
		MainStockBalance.objects.create(product_id=501, product_name='Old Name', quantity=4)

	def rename(self, name, product_id=501):
		return self.client.post(
			'/api/products/rename/',
			data=json.dumps({'product_id': product_id, 'product_name': name}),
			content_type='application/json',
		)

	def snapshot(self, name):
		return self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': 501, 'product_name': name, 'available_quantity': 4,
			}]}),
			content_type='application/json',
		)

	def test_rename_updates_every_branch_and_the_shared_pos_feed(self):
		response = self.rename('  New   Name ')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['old_name'], 'Old Name')
		self.assertEqual(set(ProductCatalog.objects.values_list('product_name', flat=True)), {'New Name'})
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=501).pending_product_name, 'New Name')
		self.assertEqual(ProductCatalog.objects.get(branch='MAIN', product_id=501).pending_product_name, 'New Name')
		self.assertEqual(MainStockBalance.objects.get(product_id=501).product_name, 'New Name')
		shared = self.client.get('/api/products/shared/').json()['products']
		self.assertEqual([item['product_name'] for item in shared if item['product_id'] == 501], ['New Name'])

	def test_pos_snapshot_with_old_name_does_not_undo_rename(self):
		self.rename('New Name')

		self.assertEqual(self.snapshot('Old Name').status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=501)
		self.assertEqual(product.product_name, 'New Name')
		self.assertEqual(product.pending_product_name, 'New Name')

	def test_pos_snapshot_with_new_name_confirms_rename(self):
		self.rename('New Name')

		self.assertEqual(self.snapshot('new name').status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=501)
		self.assertEqual(product.pending_product_name, '')
		self.assertEqual(self.snapshot('Renamed In Quantum').status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=501).product_name, 'Renamed In Quantum')

	def rename_status(self):
		response = self.client.get('/api/products/renames/?product_id=501')
		self.assertEqual(response.status_code, 200)
		renames = response.json()['renames']
		return renames, {item['branch']: item['state'] for item in renames[0]['branches']}

	def test_rename_lists_every_branch_including_offline_ones(self):
		BranchHeartbeat.objects.create(branch='BranchA', last_seen=timezone.now())
		BranchHeartbeat.objects.create(branch='BranchC', last_seen=timezone.now() - timedelta(days=2))
		self.rename('New Name')

		renames, states = self.rename_status()
		self.assertEqual(renames[0]['old_name'], 'Old Name')
		self.assertEqual(renames[0]['new_name'], 'New Name')
		# MAIN is not a branch POS; BranchC has no copy yet but is a branch that is offline.
		self.assertEqual(states, {'BranchA': 'waiting', 'BranchB': 'offline', 'BranchC': 'offline'})
		self.assertEqual(renames[0]['updated_count'], 0)

	def test_branch_snapshot_with_new_name_marks_that_branch_updated(self):
		self.rename('New Name')
		self.snapshot('Old Name')
		self.assertEqual(self.rename_status()[1]['BranchA'], 'offline')

		self.snapshot('New Name')

		renames, states = self.rename_status()
		self.assertEqual(states, {'BranchA': 'updated', 'BranchB': 'offline'})
		self.assertEqual(renames[0]['updated_count'], 1)
		self.assertIsNotNone(renames[0]['branches'][0]['confirmed_at'])

	def test_main_publish_of_a_branch_row_does_not_confirm_that_branch(self):
		self.rename('New Name')
		# The Main PC republishes branch rows it got from the website inbox.
		self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchB', 'product_id': 501, 'product_name': 'New Name', 'available_quantity': 4,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(ProductCatalog.objects.get(branch='BranchB', product_id=501).pending_product_name, 'New Name')
		self.assertEqual(self.rename_status()[1]['BranchB'], 'offline')

	def test_a_newer_rename_replaces_the_older_one(self):
		self.rename('First Name')
		self.rename('Second Name')

		renames = self.client.get('/api/products/renames/').json()['renames']
		self.assertEqual([item['new_name'] for item in renames], ['Second Name', 'First Name'])
		self.assertEqual({item['state'] for item in renames[1]['branches']}, {'replaced'})
		self.assertEqual(renames[1]['old_name'], 'Old Name')
		self.assertEqual(renames[0]['old_name'], 'First Name')

	def test_recent_renames_for_notifications_skip_old_and_replaced_ones(self):
		self.rename('First Name')
		self.rename('Second Name')
		ProductCatalog.objects.create(branch='BranchA', product_id=502, product_name='Other Old', available_quantity=1)
		self.rename('Other New', product_id=502)
		ProductRename.objects.filter(new_name='Other New').update(renamed_at=timezone.now() - timedelta(days=4))

		renames = self.client.get('/api/products/renames/?recent=1').json()['renames']
		self.assertEqual([item['new_name'] for item in renames], ['Second Name'])

	def test_pages_with_the_api_meter_load_rename_notifications(self):
		response = self.client.get('/stock/')
		self.assertContains(response, 'rename_notifications.js')
		self.assertContains(response, 'API received today')

	def test_renamed_products_page_lists_previous_and_current_names(self):
		page = self.client.get('/products/renamed/')
		self.assertContains(page, 'Renamed products')
		self.assertContains(page, 'Popup settings')
		self.assertContains(page, 'rename_notifications.js')

		self.rename('New Name')
		# A branch POS later renames it itself; the page shows the name the web has now.
		self.snapshot('New Name')
		self.snapshot('Renamed In Quantum')
		renames = self.client.get('/api/products/renames/?all=1').json()['renames']
		self.assertEqual(renames[0]['old_name'], 'Old Name')
		self.assertEqual(renames[0]['new_name'], 'New Name')
		self.assertEqual(renames[0]['current_name'], 'Renamed In Quantum')
		self.assertFalse(renames[0]['deleted'])

	def test_popup_days_setting_limits_recent_renames(self):
		self.rename('New Name')
		ProductRename.objects.update(renamed_at=timezone.now() - timedelta(days=5))

		self.assertEqual(self.client.get('/api/products/renames/?recent=1').json()['renames'], [])
		self.assertEqual(len(self.client.get('/api/products/renames/?recent=1&days=7').json()['renames']), 1)
		# Out-of-range and invalid values fall back safely.
		self.assertEqual(len(self.client.get('/api/products/renames/?recent=1&days=99').json()['renames']), 1)
		self.assertEqual(self.client.get('/api/products/renames/?recent=1&days=x').status_code, 200)

	def branch_tracking(self, branch, action):
		return self.client.post(
			'/api/products/renames/branches/',
			data=json.dumps({'branch': branch, 'action': action}),
			content_type='application/json',
		)

	def test_removed_branch_is_left_out_of_renames_and_can_be_added_back(self):
		BranchHeartbeat.objects.create(branch='OLDBRANCH', last_seen=timezone.now() - timedelta(days=30))
		self.rename('New Name')
		self.assertIn('OLDBRANCH', self.rename_status()[1])

		response = self.branch_tracking('oldbranch', 'remove')
		self.assertEqual(response.status_code, 200)
		self.assertIn('oldbranch', [item['branch'] for item in response.json()['removed_branches']])
		# Removing twice is harmless.
		self.assertEqual(self.branch_tracking('OLDBRANCH', 'remove').status_code, 200)
		self.assertEqual(RenameIgnoredBranch.objects.filter(branch__iexact='OLDBRANCH').count(), 1)

		renames, states = self.rename_status()
		self.assertNotIn('OLDBRANCH', states)
		self.assertEqual(renames[0]['branch_count'], 2)
		recent = self.client.get('/api/products/renames/?recent=1').json()['renames']
		self.assertNotIn('OLDBRANCH', [item['branch'] for item in recent[0]['branches']])
		# New renames skip it too.
		self.rename('Newer Name')
		self.assertFalse(ProductRenameBranch.objects.filter(rename__new_name='Newer Name', branch='OLDBRANCH').exists())
		# Nothing else about the branch changes.
		self.assertTrue(BranchHeartbeat.objects.filter(branch='OLDBRANCH').exists())

		self.assertEqual(self.branch_tracking('OLDBRANCH', 'restore').status_code, 200)
		self.assertFalse(RenameIgnoredBranch.objects.filter(branch__iexact='OLDBRANCH').exists())
		renames = self.client.get('/api/products/renames/?product_id=501').json()['renames']
		self.assertIn('OLDBRANCH', [item['branch'] for item in renames[1]['branches']])

	def test_branch_tracking_rejects_bad_requests(self):
		self.assertEqual(self.branch_tracking('', 'remove').status_code, 400)
		self.assertEqual(self.branch_tracking('OLDBRANCH', 'delete').status_code, 400)
		self.client.logout()
		self.assertEqual(self.branch_tracking('OLDBRANCH', 'remove').status_code, 302)
		self.assertFalse(RenameIgnoredBranch.objects.filter(branch__iexact='OLDBRANCH').exists())

	def test_old_and_test_branches_are_removed_from_renames(self):
		self.assertEqual(sorted(RenameIgnoredBranch.objects.values_list('branch', flat=True)), ['CROSSCOUNTRY', 'LAXDOM TEST', 'TEST'])
		for branch in ['CROSSCOUNTRY', 'LAXDOM TEST', 'TEST', 'CROSS COUNTRY']:
			BranchHeartbeat.objects.create(branch=branch, last_seen=timezone.now())
		self.rename('New Name')
		states = self.rename_status()[1]
		self.assertNotIn('CROSSCOUNTRY', states)
		self.assertNotIn('LAXDOM TEST', states)
		self.assertNotIn('TEST', states)
		self.assertIn('CROSS COUNTRY', states)

	def test_test_branches_migration_removes_them(self):
		from django.apps import apps
		TEST_BRANCH_MIGRATION.remove_test_branches(apps, None)
		TEST_BRANCH_MIGRATION.remove_test_branches(apps, None)
		self.assertEqual(
			sorted(RenameIgnoredBranch.objects.values_list('branch', flat=True)),
			['BranchA', 'CROSSCOUNTRY', 'LAXDOM TEST', 'Pangoline', 'TASHINGA', 'TEST'],
		)
		TEST_BRANCH_MIGRATION.add_test_branches_back(apps, None)
		self.assertFalse(RenameIgnoredBranch.objects.filter(branch='BranchA').exists())

	def test_mini_market_returns_move_to_the_sale_day(self):
		from datetime import datetime, timedelta, timezone as utc_zone
		from django.apps import apps
		done = datetime(2026, 11, 18, 5, 39, tzinfo=utc_zone.utc)
		for deletion_id, invoice, branch in [('mm1', '1050573', 'Mini Market'), ('mm2', '1050423', 'MINI MARKET'), ('other', '1050573', 'RUJEKO B'), ('mm3', '999', 'Mini Market')]:
			DeletionRecord.objects.create(deletion_id=deletion_id, invoice=invoice, branch=branch, status='processed', confirmation_timestamp=done)
		DeletionRecord.objects.update(timestamp=done)
		MINI_MARKET_RETURNS_MIGRATION.move_to_sale_day(apps, None)
		moved = done - timedelta(days=1)
		for deletion_id in ['mm1', 'mm2']:
			record = DeletionRecord.objects.get(pk=deletion_id)
			self.assertEqual((record.timestamp, record.confirmation_timestamp), (moved, moved))
		for deletion_id in ['other', 'mm3']:
			self.assertEqual(DeletionRecord.objects.get(pk=deletion_id).confirmation_timestamp, done)
		MINI_MARKET_RETURNS_MIGRATION.move_to_sale_day(apps, None)
		self.assertEqual(DeletionRecord.objects.get(pk='mm1').confirmation_timestamp, moved)
		MINI_MARKET_RETURNS_MIGRATION.move_back_to_return_day(apps, None)
		self.assertEqual(DeletionRecord.objects.get(pk='mm1').confirmation_timestamp, done)

	def test_removed_branch_is_left_out_of_deletions(self):
		password = 'ProductRenamePass4182!'
		ProductDeletionRequest.objects.create(branch='BranchA', product_id=777, product_name='Old Test', requested_by='x')
		ProductDeletionRequest.objects.create(branch='BranchB', product_id=777, product_name='Old Test', requested_by='x', status='completed')
		self.assertEqual(self.branch_tracking('brancha', 'remove').status_code, 200)

		branches = [item['branch'] for item in self.client.get('/api/products/deleted/').json()['products']]
		self.assertEqual(branches, ['BranchB'])

		response = self.client.post(
			'/api/products/delete/',
			data=json.dumps({'branch': 'BranchB', 'product_id': 501, 'password': password}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['branches'], ['BranchB'])

		DeletedProductSighting.objects.create(branch='BranchA', product_id=501, product_name='Old Name', last_seen=timezone.now())
		self.assertEqual(self.client.get('/api/products/deleted/').json()['still_active'], [])

	def force(self, rename_id, **extra):
		return self.client.post(
			'/api/products/renames/force/',
			data=json.dumps({'rename_id': rename_id, **extra}),
			content_type='application/json',
		)

	def test_force_update_resends_the_name_without_marking_it_done(self):
		self.rename('New Name')
		self.snapshot('New Name')  # BranchA reports it; BranchB never does
		rename = ProductRename.objects.get()
		ProductCatalog.objects.filter(branch='BranchB', product_id=501).update(product_name='Old Name', pending_product_name='')
		before = ProductCatalog.objects.get(branch='BranchB', product_id=501).updated_at

		response = self.force(rename.id)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['branches'], ['BranchB'])
		states = {item['branch']: (item['state'], item['forced_by']) for item in response.json()['renames'][0]['branches']}
		self.assertEqual(states['BranchA'], ('updated', ''))
		self.assertEqual(states['BranchB'][1], 'product-rename-admin')
		self.assertNotEqual(states['BranchB'][0], 'updated')
		branch_b = ProductCatalog.objects.get(branch='BranchB', product_id=501)
		self.assertEqual(branch_b.product_name, 'New Name')
		self.assertEqual(branch_b.pending_product_name, 'New Name')
		self.assertGreater(branch_b.updated_at, before)
		# The branch picks the name up from the shared catalog on its next sync...
		feed = self.client.get('/api/products/shared/', {'since': timezone.now().isoformat()}).json()['products']
		self.assertEqual([(item['product_id'], item['product_name']) for item in feed], [(501, 'New Name')])
		# ...and shows Updated only once its POS reports it.
		self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchB', 'products': [{'product_id': 501, 'product_name': 'New Name', 'available_quantity': 4}]}),
			content_type='application/json',
		)
		self.assertEqual(self.rename_status()[1]['BranchB'], 'updated')
		self.assertEqual(self.force(rename.id).json()['branches'], [])

	def test_shared_catalog_does_not_trust_a_fast_branch_clock(self):
		self.rename('New Name')
		# The branch PC runs an hour fast, so its last check claims to be in the future.
		fast_clock = (timezone.now() + timedelta(hours=1)).isoformat()
		feed = self.client.get('/api/products/shared/', {'since': fast_clock}).json()['products']
		self.assertEqual([(item['product_id'], item['product_name']) for item in feed], [(501, 'New Name')])
		# Older changes are not sent again.
		ProductCatalog.objects.update(updated_at=timezone.now() - timedelta(hours=1))
		self.assertEqual(self.client.get('/api/products/shared/', {'since': fast_clock}).json()['products'], [])

	def test_force_update_only_for_the_latest_rename(self):
		self.rename('New Name')
		self.rename('Newer Name')
		old = ProductRename.objects.get(new_name='New Name')
		self.assertEqual(self.force(old.id).status_code, 409)
		self.assertEqual(self.force(999999).status_code, 404)
		self.assertEqual(self.force(0).status_code, 404)

	def test_branch_without_the_product_at_rename_time_confirms_when_it_reports_it(self):
		BranchHeartbeat.objects.create(branch='BranchC', last_seen=timezone.now())
		self.rename('New Name')
		self.assertEqual(self.rename_status()[1]['BranchC'], 'waiting')
		self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchC', 'products': [{'product_id': 501, 'product_name': 'new  name', 'available_quantity': 2}]}),
			content_type='application/json',
		)
		self.assertEqual(self.rename_status()[1]['BranchC'], 'updated')

	def test_branch_reporting_the_old_name_stays_waiting(self):
		BranchHeartbeat.objects.create(branch='BranchC', last_seen=timezone.now())
		self.rename('New Name')
		self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchC', 'products': [{'product_id': 501, 'product_name': 'Old Name', 'available_quantity': 2}]}),
			content_type='application/json',
		)
		self.assertEqual(self.rename_status()[1]['BranchC'], 'waiting')

	def test_main_catalog_publish_with_old_name_does_not_undo_rename(self):
		self.rename('New Name')

		response = self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchB', 'product_id': 501, 'product_name': 'Old Name', 'available_quantity': 4,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchB', product_id=501).product_name, 'New Name')

	def publish_main(self, name, quantity=4):
		return self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'MAIN', 'product_id': 501, 'product_name': name, 'available_quantity': quantity,
			}]}),
			content_type='application/json',
		)

	def shared_name(self, since=None):
		url = '/api/products/shared/' + (f'?since={since}' if since else '')
		products = self.client.get(url).json()['products']
		return [item['product_name'] for item in products if item['product_id'] == 501]

	def test_main_pos_publishing_its_old_name_does_not_undo_rename(self):
		self.rename('New Name')

		# The Main POS never pulls renames; it keeps publishing its own name,
		# e.g. after every sale or stock change at main.
		self.assertEqual(self.publish_main('Old Name').status_code, 200)
		self.assertEqual(self.publish_main('Old Name', quantity=3).status_code, 200)

		self.assertEqual(ProductCatalog.objects.get(branch='MAIN', product_id=501).product_name, 'New Name')
		self.assertEqual(self.shared_name(), ['New Name'])

	def test_offline_branch_gets_the_new_name_when_it_comes_back(self):
		before_rename = (timezone.now() - timedelta(minutes=5)).isoformat()
		self.rename('New Name')
		self.publish_main('Old Name', quantity=2)
		# BranchA synced the rename; BranchB was offline the whole time.
		self.snapshot('New Name')

		# BranchB reconnects and asks for everything changed since it went offline.
		self.assertEqual(self.shared_name(since=before_rename.replace('+', '%2B')), ['New Name'])
		# ...and a restarted branch app (full download) gets it too.
		self.assertEqual(self.shared_name(), ['New Name'])
		branch_b = ProductCatalog.objects.get(branch='BranchB', product_id=501)
		self.assertEqual(branch_b.pending_product_name, 'New Name')

	def test_shared_feed_prefers_a_pending_rename_over_a_reverted_main_row(self):
		# State left by renames made before this fix: MAIN row reverted by a Main POS publish.
		ProductCatalog.objects.filter(product_id=501).exclude(branch='MAIN').update(product_name='New Name', pending_product_name='New Name')

		self.assertEqual(self.shared_name(), ['New Name'])

	def test_rename_rejects_blank_names_and_deleted_products(self):
		self.assertEqual(self.rename('   ').status_code, 400)
		self.assertEqual(self.rename('x' * 251).status_code, 400)
		self.assertEqual(self.rename('New Name', product_id=999999).status_code, 404)
		ProductDeletionRequest.objects.create(branch='BranchA', product_id=501, product_name='Old Name', status='completed')
		self.assertEqual(self.rename('New Name').status_code, 409)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=501).product_name, 'Old Name')


class DeletedProductStillActiveTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(username='still-active-admin', password='StillActivePass4182!')
		self.client.force_login(self.admin)
		ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=601, product_name='Ghost Product', status='completed',
			product_code='602', barcode='6006', selling_price='3.50', tax_rate='15.00',
		)

	def snapshot(self, branch, quantity=7):
		return self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': branch, 'products': [{
				'product_id': 601, 'product_name': 'Ghost Product', 'available_quantity': quantity,
			}]}),
			content_type='application/json',
		)

	def still_active(self):
		return self.client.get('/api/products/deleted/').json()['still_active']

	def test_snapshot_records_where_a_deleted_product_is_still_active(self):
		self.assertEqual(self.snapshot('BranchB').status_code, 200)
		self.assertEqual(self.snapshot('BranchB', quantity=5).status_code, 200)

		self.assertFalse(ProductCatalog.objects.filter(product_id=601).exists())
		active = self.still_active()
		self.assertEqual(len(active), 1)
		self.assertEqual(active[0]['product_id'], 601)
		self.assertEqual([item['branch'] for item in active[0]['branches']], ['BranchB'])
		self.assertEqual(active[0]['branches'][0]['available_quantity'], '5')
		# Queued again automatically - nobody has to press "Delete at these branches".
		self.assertTrue(active[0]['branches'][0]['deletion_pending'])
		self.assertTrue(active[0]['branches'][0]['auto_sync'])
		auto = ProductDeletionRequest.objects.get(branch='BranchB', product_id=601)
		self.assertEqual(auto.status, 'pending')
		self.assertEqual(auto.requested_by, 'auto-sync')
		self.assertEqual(auto.product_code, '602')
		self.assertEqual(ProductDeletionRequest.objects.filter(branch='BranchB').count(), 1)

	def test_main_snapshot_is_not_recorded(self):
		self.snapshot('MAIN')

		self.assertEqual(self.still_active(), [])

	def fail_auto_deletion(self, branch):
		deletion = ProductDeletionRequest.objects.get(branch=branch, product_id=601, status='pending')
		return self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({'request_id': deletion.pk, 'branch': branch, 'success': False, 'error': 'POS database locked'}),
			content_type='application/json',
		)

	def test_delete_again_requires_password_and_retries_only_failed_branches(self):
		self.snapshot('BranchB')
		self.snapshot('BranchC')
		self.fail_auto_deletion('BranchB')

		branch_b = next(item for item in self.still_active()[0]['branches'] if item['branch'] == 'BranchB')
		self.assertTrue(branch_b['deletion_failed'])
		self.assertEqual(branch_b['error'], 'POS database locked')

		wrong = self.client.post(
			'/api/products/deleted/delete-again/',
			data=json.dumps({'product_id': 601, 'password': 'wrong'}),
			content_type='application/json',
		)
		self.assertEqual(wrong.status_code, 403)

		response = self.client.post(
			'/api/products/deleted/delete-again/',
			data=json.dumps({'product_id': 601, 'password': 'StillActivePass4182!'}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['branches'], ['BranchB'])
		queued = ProductDeletionRequest.objects.filter(branch='BranchB', product_id=601, status='pending').get()
		self.assertEqual(queued.requested_by, 'still-active-admin')
		self.assertEqual(queued.product_code, '602')
		sync = self.client.get('/api/branch-sync/?branch=BranchB').json()
		self.assertEqual([item['product_id'] for item in sync['pending_product_deletions']], [601])
		self.assertTrue(all(branch['deletion_pending'] for branch in self.still_active()[0]['branches']))

	def test_failed_branch_is_not_retried_automatically(self):
		self.snapshot('BranchB')
		self.fail_auto_deletion('BranchB')

		self.snapshot('BranchB')

		self.assertFalse(ProductDeletionRequest.objects.filter(branch='BranchB', status='pending').exists())

	def test_branch_still_reporting_after_completion_is_retried_after_cooldown(self):
		self.snapshot('BranchB')
		deletion = ProductDeletionRequest.objects.get(branch='BranchB', product_id=601, status='pending')
		self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({'request_id': deletion.pk, 'branch': 'BranchB', 'success': True}),
			content_type='application/json',
		)

		# A snapshot taken before the POS applied it: no immediate duplicate.
		self.snapshot('BranchB')
		self.assertFalse(ProductDeletionRequest.objects.filter(branch='BranchB', status='pending').exists())

		# Still active 30+ minutes later: send the deletion again.
		ProductDeletionRequest.objects.filter(pk=deletion.pk).update(updated_at=timezone.now() - timedelta(minutes=31))
		self.snapshot('BranchB')
		self.assertEqual(ProductDeletionRequest.objects.filter(branch='BranchB', status='pending').count(), 1)

	def test_automatic_retries_stop_after_three_and_ask_for_a_person(self):
		for attempt in range(4):
			self.snapshot('BranchB')
			deletion = ProductDeletionRequest.objects.filter(branch='BranchB', product_id=601, status='pending').first()
			if deletion is None:
				break
			self.client.post(
				'/api/products/delete/complete/',
				data=json.dumps({'request_id': deletion.pk, 'branch': 'BranchB', 'success': True}),
				content_type='application/json',
			)
			ProductDeletionRequest.objects.filter(pk=deletion.pk).update(updated_at=timezone.now() - timedelta(minutes=31))

		self.snapshot('BranchB')

		self.assertEqual(ProductDeletionRequest.objects.filter(branch='BranchB', requested_by='auto-sync').count(), 3)
		self.assertFalse(ProductDeletionRequest.objects.filter(branch='BranchB', status='pending').exists())
		self.assertTrue(self.still_active()[0]['branches'][0]['auto_gave_up'])

	def test_branch_with_running_stock_transfer_waits(self):
		StockTransfer.objects.create(transfer_id='busy-601', branch='BranchB', product_id=601, quantity=1, status='pending')

		self.snapshot('BranchB')

		self.assertFalse(ProductDeletionRequest.objects.filter(branch='BranchB').exists())

	def test_deleted_page_catches_up_older_sightings(self):
		# Recorded before automatic re-deletion existed.
		DeletedProductSighting.objects.create(branch='BranchD', product_id=601, product_name='Ghost Product', available_quantity=3, last_seen=timezone.now())
		cache.clear()

		active = self.still_active()

		self.assertEqual(ProductDeletionRequest.objects.get(branch='BranchD', product_id=601).requested_by, 'auto-sync')
		self.assertTrue(active[0]['branches'][0]['deletion_pending'])

	def test_restored_product_is_not_deleted_again(self):
		self.client.post('/api/products/restore/', data=json.dumps({'product_id': 601}), content_type='application/json')

		self.snapshot('BranchB')

		self.assertFalse(ProductDeletionRequest.objects.filter(branch='BranchB', status='pending').exists())

	def test_branch_completing_the_deletion_clears_it(self):
		self.snapshot('BranchB')
		deletion = ProductDeletionRequest.objects.create(branch='BranchB', product_id=601, product_name='Ghost Product')

		complete = self.client.post(
			'/api/products/delete/complete/',
			data=json.dumps({'request_id': deletion.pk, 'branch': 'BranchB', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(complete.status_code, 200)
		self.assertEqual(self.still_active(), [])

	def test_restore_clears_it_and_the_branch_snapshot_shows_the_product_again(self):
		self.snapshot('BranchB')

		restore = self.client.post(
			'/api/products/restore/',
			data=json.dumps({'product_id': 601}),
			content_type='application/json',
		)

		self.assertEqual(restore.status_code, 202)
		self.assertFalse(DeletedProductSighting.objects.exists())
		self.assertEqual(self.still_active(), [])
		self.snapshot('BranchB')
		self.assertFalse(DeletedProductSighting.objects.exists())

	def test_deleted_products_page_has_still_active_panel(self):
		page = self.client.get('/products/deleted/')

		self.assertContains(page, 'Still on branch POS')
		self.assertContains(page, 'Delete at these branches')
		self.assertContains(page, 'Search deleted products')
		self.assertContains(page, 'Deleting automatically')


class StockTransferTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(
			username='transfer-admin', password='TransferPass4182!'
		)
		self.client.force_login(self.admin)
		MainStockBalance.objects.create(product_id=999, product_name='Test Product', quantity=10)

	def test_sign_out_works_from_each_page_with_csrf_enforced(self):
		client = self.client_class(enforce_csrf_checks=True)
		for page in ('/', '/stock/', '/stock/product-matrix/', '/history/', '/stock/movements/history/', '/stock/movement-adjustments/', '/users/'):
			client.force_login(self.admin)
			page_response = client.get(page)
			self.assertEqual(page_response.status_code, 200, page)
			logout_form = re.search(
				r'<form[^>]*action="/logout/"[^>]*>(.*?)</form>',
				page_response.content.decode(),
				re.DOTALL,
			)
			self.assertIsNotNone(logout_form, page)
			csrf_token = re.search(
				r'name="csrfmiddlewaretoken" value="([^"]+)"',
				logout_form.group(1),
			)
			self.assertIsNotNone(csrf_token, page)
			logout_response = client.post('/logout/', {'csrfmiddlewaretoken': csrf_token.group(1)})
			self.assertRedirects(logout_response, '/login/', fetch_redirect_response=False)

	def test_product_stock_matrix_shows_only_online_branches_and_confirmed_quantities(self):
		ProductCatalog.objects.create(
			branch='MAIN', product_id=4100, product_name='Main catalog product',
		)
		ProductCatalog.objects.create(
			branch='BranchA', product_id=4100, product_name='Branch product',
			available_quantity=12, branch_confirmed=True,
		)
		ProductCatalog.objects.create(
			branch='BranchA', product_id=4200, product_name='Pending product',
			available_quantity=50, branch_confirmed=False,
		)
		ProductCatalog.objects.create(
			branch='BranchA', product_id=4300, product_name='Deleting product',
			available_quantity=8, branch_confirmed=True,
		)
		ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=4300, product_name='Deleting product',
			status='pending',
		)
		BranchHeartbeat.objects.create(branch='BranchA', last_seen=timezone.now())
		BranchHeartbeat.objects.create(
			branch='OfflineBranch', last_seen=timezone.now() - timedelta(hours=1),
		)

		page = self.client.get('/stock/product-matrix/')
		self.assertEqual(page.status_code, 200)
		self.assertContains(page, 'Branch stock matrix')
		self.assertContains(page, '/api/stock/product-matrix/')
		self.assertContains(page, 'Submit stock updates')
		self.assertContains(page, '<option value="" selected>Select update type</option>', html=True)
		self.assertContains(page, 'Stocktake — replace current quantity')

		response = self.client.get('/api/stock/product-matrix/')
		self.assertEqual(response.status_code, 200)
		data = response.json()
		branch_by_name = {branch['name']: branch for branch in data['branches']}
		self.assertTrue(branch_by_name['BranchA']['online'])
		self.assertNotIn('OfflineBranch', branch_by_name)
		product_by_id = {product['product_id']: product for product in data['products']}
		self.assertEqual(product_by_id[4100]['product_name'], 'Main catalog product')
		self.assertEqual(product_by_id[4100]['quantities']['brancha'], '12')
		self.assertNotIn('offlinebranch', product_by_id[4100]['quantities'])
		self.assertNotIn(4200, product_by_id)
		self.assertNotIn(4300, product_by_id)

	def test_stock_summary_ajax_returns_removals_for_pending_and_completed_deletions(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=4400, product_name='Deleting product',
			available_quantity=8, branch_confirmed=True,
		)
		deletion = ProductDeletionRequest.objects.create(
			branch='BranchA', product_id=4400, product_name='Deleting product',
			status='pending',
		)

		pending_response = self.client.get('/api/stock/summary/?branch=BranchA')
		self.assertEqual(pending_response.status_code, 200)
		self.assertNotIn(4400, [item['product_id'] for item in pending_response.json()['products']])

		deletion.status = 'completed'
		deletion.save(update_fields=['status', 'updated_at'])
		ProductCatalog.objects.filter(branch='BranchA', product_id=4400).delete()
		since = (timezone.now() - timedelta(minutes=1)).isoformat()
		response = self.client.get('/api/stock/summary/', {'branch': 'BranchA', 'since': since})
		self.assertEqual(response.status_code, 200)
		self.assertIn(4400, response.json()['removed_product_ids'])

	def test_product_stock_matrix_api_requires_login(self):
		self.client.logout()
		response = self.client.get('/api/stock/product-matrix/')
		self.assertEqual(response.status_code, 302)

	def test_branch_matrix_submits_stocktakes_and_additions_as_one_batch(self):
		BranchHeartbeat.objects.create(branch='BranchA', last_seen=timezone.now())
		BranchHeartbeat.objects.create(branch='BranchB', last_seen=timezone.now())
		ProductCatalog.objects.create(
			branch='BranchA', product_id=2401, product_name='Counted product', available_quantity=12,
		)
		ProductCatalog.objects.create(
			branch='BranchB', product_id=2402, product_name='New stock product', available_quantity=5,
		)
		main_balance = MainStockBalance.objects.get(product_id=999).quantity

		response = self.client.post(
			'/api/stock/branch/updates/',
			data=json.dumps({
				'updates': [
					{
						'branch': 'BranchA', 'product_id': 2401, 'product_name': 'Counted product',
						'mode': 'stocktake', 'quantity': '8',
					},
					{
						'branch': 'BranchB', 'product_id': 2402, 'product_name': 'New stock product',
						'mode': 'add', 'quantity': '3',
					},
				],
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(response.json()['queued'], 2)
		self.assertEqual(response.json()['status'], 'accepted')
		transfers = {
			(transfer.branch, transfer.product_id): transfer
			for transfer in StockTransfer.objects.order_by('product_id')
		}
		stocktake = transfers[('BranchA', 2401)]
		self.assertEqual(stocktake.quantity, 8)
		self.assertEqual(stocktake.target_quantity, 8)
		addition = transfers[('BranchB', 2402)]
		self.assertEqual(addition.quantity, 3)
		self.assertIsNone(addition.target_quantity)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchB', product_id=2402).available_quantity, 5)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, main_balance)

	def test_branch_matrix_batch_rejects_offline_branch_without_partial_updates(self):
		BranchHeartbeat.objects.create(
			branch='OfflineBranch',
			last_seen=timezone.now() - timedelta(hours=1),
		)
		ProductCatalog.objects.create(
			branch='OfflineBranch', product_id=2403, product_name='Offline product',
		)

		response = self.client.post(
			'/api/stock/branch/updates/',
			data=json.dumps({
				'updates': [{
					'branch': 'OfflineBranch', 'product_id': 2403, 'product_name': 'Offline product',
					'mode': 'stocktake', 'quantity': '0',
				}],
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 409)
		self.assertIn('offline', response.json()['message'])
		self.assertFalse(StockTransfer.objects.exists())

	@override_settings(WHATSAPP_STOCK_API_TOKEN='test-whatsapp-token', WHATSAPP_STOCK_TIME_ZONE='UTC')
	def test_whatsapp_stock_lookup_requires_token_and_filters_transfers_by_branch(self):
		StockTransfer.objects.create(
			transfer_id='WA_BRANCH_A', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=4, status='completed',
			completed_at=timezone.now() - timedelta(days=1),
		)
		StockTransfer.objects.create(
			transfer_id='WA_BRANCH_B', branch='BranchB', product_id=999,
			product_name='Other Product', quantity=2, status='pending',
		)
		StockTransfer.objects.create(
			transfer_id='WA_YESTERDAY', branch='BranchA', product_id=999,
			product_name='Yesterday Product', quantity=8, status='completed',
			completed_at=timezone.now(),
		)
		StockTransfer.objects.filter(pk='WA_YESTERDAY').update(created_at=timezone.now() - timedelta(days=1))
		StockTransfer.objects.create(
			transfer_id='WA_PENDING', branch='BranchA', product_id=999,
			product_name='Pending Product', quantity=9, status='pending',
		)
		StockTransfer.objects.create(
			transfer_id='STOCKTAKE_TODAY', branch='BranchA', product_id=999,
			product_name='Stock correction', quantity=3, status='completed',
		)

		unauthorized = self.client.get('/api/whatsapp/stock/?branch=BranchA')
		self.assertEqual(unauthorized.status_code, 403)

		response = self.client.get(
			'/api/whatsapp/stock/?branch=brancha',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(response.status_code, 200)
		self.assertCountEqual(
			[row['transfer_id'] for row in response.json()['transfers']],
			['WA_BRANCH_A', 'WA_PENDING'],
		)
		self.assertEqual(response.json()['date'], timezone.localdate().isoformat())
		self.assertIn('sent_at', response.json()['transfers'][0])
		self.assertNotIn('received_at', response.json()['transfers'][0])

		yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
		historical = self.client.get(
			f'/api/whatsapp/stock/?branch=BranchA&date={yesterday}',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(historical.status_code, 200)
		self.assertEqual(historical.json()['date'], yesterday)
		self.assertEqual([row['transfer_id'] for row in historical.json()['transfers']], ['WA_YESTERDAY'])

		invalid_date = self.client.get(
			'/api/whatsapp/stock/?branch=BranchA&date=not-a-date',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(invalid_date.status_code, 400)

	@override_settings(WHATSAPP_STOCK_API_TOKEN='test-whatsapp-token', WHATSAPP_STOCK_TIME_ZONE='UTC')
	def test_whatsapp_stock_lookup_returns_received_movements_for_branch_and_date(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Received Product',
			movement_type='received', quantity=6, source='POS receiving',
		)
		yesterday_receipt = StockMovement.objects.create(
			branch='BranchA', product_id=1000, product_name='Yesterday Receipt',
			movement_type='received', quantity=8, source='POS receiving',
		)
		StockMovement.objects.filter(pk=yesterday_receipt.pk).update(
			created_at=timezone.now() - timedelta(days=1),
		)
		StockMovement.objects.create(
			branch='BranchB', product_id=1001, product_name='Other Branch Receipt',
			movement_type='received', quantity=10, source='POS receiving',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1002, product_name='Sold Product',
			movement_type='sold', quantity=2, source='POS sale',
		)

		response = self.client.get(
			'/api/whatsapp/stock/?action=received&branch=brancha',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['date'], timezone.localdate().isoformat())
		self.assertEqual(
			[row['product_name'] for row in response.json()['receipts']],
			['Received Product'],
		)
		self.assertEqual(response.json()['receipts'][0]['quantity'], '6')
		self.assertIn('received_at', response.json()['receipts'][0])

		yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
		historical = self.client.get(
			f'/api/whatsapp/stock/?action=received&branch=BranchA&date={yesterday}',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(historical.status_code, 200)
		self.assertEqual(
			[row['product_name'] for row in historical.json()['receipts']],
			['Yesterday Receipt'],
		)

	@override_settings(WHATSAPP_STOCK_API_TOKEN='test-whatsapp-token')
	def test_whatsapp_stock_lookup_rejects_unknown_action_and_invalid_date(self):
		unknown_action = self.client.get(
			'/api/whatsapp/stock/?action=unknown&branch=BranchA',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(unknown_action.status_code, 400)

		invalid_date = self.client.get(
			'/api/whatsapp/stock/?action=received&branch=BranchA&date=not-a-date',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)
		self.assertEqual(invalid_date.status_code, 400)

	@override_settings(WHATSAPP_STOCK_API_TOKEN='test-whatsapp-token')
	def test_whatsapp_branch_list_includes_required_branches_and_live_branches(self):
		ProductCatalog.objects.create(branch='Extra Branch', product_id=8101, product_name='Catalog product')
		ProductCatalog.objects.create(branch='sisk', product_id=8102, product_name='SISK product')
		ProductCatalog.objects.create(branch='MAIN', product_id=8103, product_name='Main product')
		StockTransfer.objects.create(
			transfer_id='WA_BRANCH_ONLY',
			branch='Transfer Only Branch',
			product_id=8104,
			product_name='Transfer product',
			quantity=1,
		)

		response = self.client.get(
			'/api/whatsapp/stock/?action=branches',
			HTTP_X_WHATSAPP_TOKEN='test-whatsapp-token',
		)

		self.assertEqual(response.status_code, 200)
		branches = response.json()['branches']
		for branch in (
			'COMPLEX', 'CROSS COUNTRY', 'DZOVANI', 'INVESTMENTS',
			'Mini Market', 'PERFECT', 'RUJEKO B', 'SISK', 'The Place', 'VEES',
			'WEST', 'YEUKAI', 'ZBS', 'Extra Branch', 'Transfer Only Branch',
		):
			with self.subTest(branch=branch):
				self.assertIn(branch, branches)
		self.assertEqual(sum(branch.casefold() == 'sisk' for branch in branches), 1)
		self.assertNotIn('MAIN', branches)

	@override_settings(STORAGES={
		'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
		'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
	})
	def test_bandwidth_page_and_dashboard_link(self):
		self.client.logout()
		self.assertEqual(self.client.get('/bandwidth/').status_code, 302)
		self.client.force_login(self.admin)
		self.assertContains(self.client.get('/'), 'id="api-bandwidth-meter" href="/bandwidth/"')
		page = self.client.get('/bandwidth/')
		self.assertContains(page, 'API data received today')
		self.assertContains(page, 'Daily log')
		self.assertContains(page, 'data-bandwidth-month-log')

	def test_main_stock_update_sets_stock_take_value(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=7,
		)
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 20, 'branch': 'BranchA'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['quantity'], '20')
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 20)
		stock_take = StockTransfer.objects.get()
		self.assertEqual(stock_take.target_quantity, 20)
		self.assertEqual(stock_take.quantity, 20)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 7)
		self.assertFalse(StockMovement.objects.filter(product_id=999, branch='BranchA').exists())
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 20)
		movement = StockMovement.objects.get(product_id=999, branch='BranchA')
		self.assertEqual(movement.movement_type, 'stocktake')
		self.assertEqual(movement.quantity, 20)
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(StockMovement.objects.filter(product_id=999, branch='BranchA').count(), 1)

	def test_branch_stocktake_all_queues_each_branch_balance_without_changing_main_stock(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1001, product_name='Two in stock', available_quantity=2)
		ProductCatalog.objects.create(branch='BranchA', product_id=1002, product_name='Out of stock', available_quantity=0)
		ProductCatalog.objects.create(branch='BranchA', product_id=1003, product_name='Already queued', available_quantity=5)
		ProductCatalog.objects.create(branch='BranchA', product_id=1004, product_name='Negative stock', available_quantity=-1)
		ProductCatalog.objects.create(branch='BranchB', product_id=1005, product_name='Other branch', available_quantity=8)
		StockTransfer.objects.create(
			transfer_id='STOCKTAKE_BranchA_1003_existing', branch='BranchA', product_id=1003,
			product_name='Already queued', quantity=5, target_quantity=5,
		)

		response = self.client.post(
			'/api/stock/branch/stocktake-all/',
			data=json.dumps({'branch': 'BranchA', 'pin': 'TransferPass4182!'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['queued'], 3)
		self.assertEqual(response.json()['skipped_pending'], 1)
		self.assertEqual(response.json()['negative_zeroed'], 1)
		batch_id = response.json()['batch_id']
		queued = StockTransfer.objects.filter(branch='BranchA', target_quantity__isnull=False).exclude(product_id=1003)
		self.assertEqual(
			list(queued.order_by('product_id').values_list('product_id', 'quantity', 'target_quantity')),
			[(1001, 2, 2), (1002, 0, 0), (1004, 0, 0)],
		)
		self.assertFalse(MainStockBalance.objects.filter(product_id__in=[1001, 1002, 1003, 1004]).exists())
		self.assertFalse(StockMovement.objects.exists())
		progress = self.client.get(f'/api/stock/branch/stocktake-batches/{batch_id}/')
		self.assertEqual(progress.status_code, 200)
		self.assertEqual(progress.json()['counts']['pending'], 3)
		self.assertEqual(progress.json()['total'], 3)

	def test_branch_stocktake_all_requires_a_branch(self):
		response = self.client.post(
			'/api/stock/branch/stocktake-all/',
			data=json.dumps({'pin': 'TransferPass4182!'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Branch is required.')

	def test_branch_stocktake_all_requires_the_signed_in_users_pin(self):
		for pin in ('', 'incorrect-pin'):
			with self.subTest(pin='missing' if not pin else 'incorrect'):
				response = self.client.post(
					'/api/stock/branch/stocktake-all/',
					data=json.dumps({'branch': 'BranchA', 'pin': pin}),
					content_type='application/json',
				)
				self.assertEqual(response.status_code, 403)
		self.assertFalse(StockTransfer.objects.exists())

	def test_stocktake_batch_can_cancel_unclaimed_products_only(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1101, product_name='First Product', available_quantity=2)
		ProductCatalog.objects.create(branch='BranchA', product_id=1102, product_name='Second Product', available_quantity=3)
		ProductCatalog.objects.create(branch='BranchA', product_id=1103, product_name='Claimed Product', available_quantity=4)
		queue_response = self.client.post(
			'/api/stock/branch/stocktake-all/',
			data=json.dumps({'branch': 'BranchA', 'pin': 'TransferPass4182!'}),
			content_type='application/json',
		)
		batch_id = queue_response.json()['batch_id']
		batch_prefix = f'STOCKTAKE_BULK_{batch_id}_'
		transfers = list(StockTransfer.objects.filter(transfer_id__startswith=batch_prefix).order_by('product_id'))
		StockTransfer.objects.filter(pk=transfers[2].pk).update(claimed_at=timezone.now())
		cancel_one_response = self.client.post(
			f'/api/stock/branch/stocktake-batches/{batch_id}/',
			data=json.dumps({'transfer_id': transfers[0].transfer_id}),
			content_type='application/json',
		)
		self.assertEqual(cancel_one_response.status_code, 200)
		self.assertEqual(cancel_one_response.json()['cancelled_now'], 1)

		cancel_response = self.client.post(f'/api/stock/branch/stocktake-batches/{batch_id}/', data=json.dumps({}), content_type='application/json')

		self.assertEqual(cancel_response.status_code, 200)
		self.assertEqual(cancel_response.json()['cancelled_now'], 1)
		self.assertEqual(cancel_response.json()['counts']['in_progress'], 1)
		self.assertEqual(cancel_response.json()['counts']['cancelled'], 2)
		self.assertEqual(cancel_response.json()['counts']['pending'], 0)
		product_states = {product['product_id']: product for product in cancel_response.json()['products']}
		self.assertFalse(product_states[transfers[2].product_id]['can_cancel'])
		self.assertEqual(product_states[transfers[2].product_id]['status'], 'in_progress')

	def test_movement_adjustment_history_shows_actor_times_and_only_adjustments(self):
		completed_at = timezone.now()
		StockTransfer.objects.create(
			transfer_id='STOCKTAKE_BULK_AUDIT_COMPLETE', branch='BranchA', product_id=1201,
			product_name='Audit Product', quantity=0, target_quantity=0, created_by='transfer-admin',
			status='completed', completed_at=completed_at,
		)
		StockTransfer.objects.create(
			transfer_id='STOCKTAKE_BULK_AUDIT_PENDING', branch='BranchA', product_id=1202,
			product_name='Pending Audit Product', quantity=5, target_quantity=5, created_by='transfer-admin',
			status='pending', claimed_at=timezone.now(),
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_AUDIT_EXCLUDED', branch='BranchA', product_id=1203,
			product_name='Regular Transfer', quantity=5, created_by='transfer-admin',
		)

		page = self.client.get('/stock/movement-adjustments/')
		response = self.client.get('/api/stock/movement-adjustments/history/?branch=BranchA&q=transfer-admin')

		self.assertEqual(page.status_code, 200)
		self.assertContains(page, 'Movement adjustment history')
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['total'], 2)
		adjustments = {row['product_id']: row for row in response.json()['adjustments']}
		self.assertEqual(adjustments[1201]['created_by'], 'transfer-admin')
		self.assertEqual(adjustments[1201]['quantity'], '0')
		self.assertTrue(adjustments[1201]['created_at'])
		self.assertEqual(adjustments[1201]['completed_at'], completed_at.isoformat())
		self.assertEqual(adjustments[1201]['status'], 'completed')
		self.assertEqual(adjustments[1202]['status'], 'in_progress')

	def test_stock_take_completion_does_not_duplicate_legacy_movement(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=7,
		)
		self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 20, 'branch': 'BranchA'}),
			content_type='application/json',
		)
		transfer = StockTransfer.objects.get()
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='stocktake', quantity=20, source=f'{transfer.created_by} (stock take)',
		)

		response = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(StockMovement.objects.filter(product_id=999, branch='BranchA').count(), 1)

	def test_stock_receipt_adds_new_stock_but_stock_take_overrides_branch_quantity(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1000, product_name='Batch Product', available_quantity=7)
		normal_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_ADD_1000', branch='BranchA', product_id=1000,
			product_name='Batch Product', quantity=5,
		)

		response = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': normal_transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=1000).available_quantity, 12)

		stock_take_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_TAKE_1000', branch='BranchA', product_id=1000,
			product_name='Batch Product', quantity=20, target_quantity=20,
		)
		response = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take_transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=1000).available_quantity, 20)

	def test_main_stock_update_rejects_negative_stock_take(self):
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': -1}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 10)

	def test_transfer_is_only_claimed_by_its_branch(self):
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 5}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)

		wrong_branch = self.client.get('/api/branch-sync/?branch=TASHINGA')
		self.assertEqual(wrong_branch.json()['pending_transfers'], [])
		self.assertEqual(StockTransfer.objects.get().status, 'pending')

		branch_response = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(branch_response.json()['pending_transfers'][0]['branch'], 'BranchA')
		self.assertEqual(StockTransfer.objects.get().status, 'pending')
		self.assertIsNotNone(StockTransfer.objects.get().claimed_at)
		second_poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(second_poll.json()['pending_transfers'], [])

	def test_pending_transfer_queue_skips_failed_item_during_retry_backoff(self):
		first = StockTransfer.objects.create(
			transfer_id='TRANSFER_FIRST', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_SECOND', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=3,
		)

		first_poll = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(first_poll.json()['pending_transfers'][0]['id'], first.transfer_id)
		self.assertEqual(
			self.client.get('/api/stock/transfers/pending/?branch=BranchA').json()['pending_transfers'],
			[],
		)

		failed_response = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': first.transfer_id, 'branch': 'BranchA', 'success': False}),
			content_type='application/json',
		)
		self.assertEqual(failed_response.status_code, 200)
		first.refresh_from_db()
		self.assertEqual(first.retry_count, 1)
		self.assertGreater(first.retry_after, timezone.now() + timedelta(minutes=4))
		self.assertLess(first.retry_after, timezone.now() + timedelta(minutes=6))
		next_poll = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(next_poll.json()['pending_transfers'][0]['id'], 'TRANSFER_SECOND')
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': 'TRANSFER_SECOND', 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		StockTransfer.objects.filter(pk=first.pk).update(retry_after=timezone.now() - timedelta(seconds=1))
		retry_poll = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(retry_poll.json()['pending_transfers'][0]['id'], first.transfer_id)
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': first.transfer_id, 'branch': 'BranchA', 'success': False}),
			content_type='application/json',
		)
		first.refresh_from_db()
		self.assertEqual(first.retry_count, 2)
		self.assertGreater(first.retry_after, timezone.now() + timedelta(minutes=9))
		self.assertLess(first.retry_after, timezone.now() + timedelta(minutes=11))
		StockTransfer.objects.filter(pk=first.pk).update(retry_after=timezone.now() - timedelta(seconds=1))
		retry_poll = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(retry_poll.json()['pending_transfers'][0]['id'], first.transfer_id)

		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': first.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		first.refresh_from_db()
		self.assertEqual(first.retry_count, 0)
		self.assertIsNone(first.retry_after)
		next_poll = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(next_poll.json()['pending_transfers'], [])

	def test_stale_pending_transfers_are_retired_after_retry_limit(self):
		stale = StockTransfer.objects.create(
			transfer_id='TRANSFER_STALE', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
			retry_count=6,
			retry_after=timezone.now() - timedelta(hours=2),
		)
		current = StockTransfer.objects.create(
			transfer_id='TRANSFER_CURRENT', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=3,
		)

		response = self.client.get('/api/stock/transfers/pending/?branch=BranchA')
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['pending_transfers'][0]['id'], current.transfer_id)
		stale.refresh_from_db()
		self.assertEqual(stale.status, 'failed')
		self.assertIsNone(stale.retry_after)

	def test_admin_can_only_delete_selected_pending_mini_market_transfers(self):
		pending = StockTransfer.objects.create(
			transfer_id='TRANSFER_MINI_PENDING', branch='Mini Market', product_id=999,
			product_name='Test Product', quantity=5,
		)
		other_branch = StockTransfer.objects.create(
			transfer_id='TRANSFER_OTHER_BRANCH', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=3,
		)
		completed = StockTransfer.objects.create(
			transfer_id='TRANSFER_MINI_COMPLETED', branch='Mini Market', product_id=999,
			product_name='Test Product', quantity=2, status='completed',
		)
		admin_url = '/admin/loraApi/stocktransfer/'
		selected_ids = [pending.pk, other_branch.pk, completed.pk]
		post_data = {
			'action': 'delete_pending_mini_market_transfers',
			'index': '0',
			'_selected_action': selected_ids,
		}

		confirmation = self.client.post(admin_url, post_data)
		self.assertEqual(confirmation.status_code, 200)
		self.assertContains(confirmation, 'Are you sure')

		post_data['post'] = 'yes'
		response = self.client.post(admin_url, post_data)
		self.assertEqual(response.status_code, 302)
		self.assertFalse(StockTransfer.objects.filter(pk=pending.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=other_branch.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=completed.pk).exists())

	def test_admin_can_delete_selected_pending_yeukai_transfers_but_not_stocktakes(self):
		pending_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_4075_TEST', branch='YEUKAI', product_id=4075,
			product_name='HONEYPOT MAHEU', quantity=100,
		)
		stocktake = StockTransfer.objects.create(
			transfer_id='STOCKTAKE_YEUKAI_4367_TEST', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, target_quantity=240,
		)
		other_branch = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCH_4075_TEST', branch='BranchA', product_id=4075,
			product_name='HONEYPOT MAHEU', quantity=100,
		)
		completed = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_4588_DONE', branch='YEUKAI', product_id=4588,
			product_name='GAMBUKAMBU', quantity=60, status='completed',
		)
		admin_url = '/admin/loraApi/stocktransfer/'
		post_data = {
			'action': 'delete_pending_yeukai_transfers',
			'index': '0',
			'_selected_action': [pending_transfer.pk, stocktake.pk, other_branch.pk, completed.pk],
		}

		confirmation = self.client.post(admin_url, post_data)
		self.assertEqual(confirmation.status_code, 200)
		self.assertContains(confirmation, 'Are you sure')

		post_data['post'] = 'yes'
		response = self.client.post(admin_url, post_data)
		self.assertEqual(response.status_code, 302)
		self.assertFalse(StockTransfer.objects.filter(pk=pending_transfer.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=stocktake.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=other_branch.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=completed.pk).exists())

	def test_admin_can_delete_only_selected_pending_yeukai_stocktakes(self):
		stocktake = StockTransfer.objects.create(
			transfer_id='STOCKTAKE_YEUKAI_4367_DELETE', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, target_quantity=240,
		)
		pending_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_4367_KEEP', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240,
		)
		other_branch = StockTransfer.objects.create(
			transfer_id='STOCKTAKE_BRANCH_4367_KEEP', branch='BranchA', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, target_quantity=240,
		)
		completed = StockTransfer.objects.create(
			transfer_id='STOCKTAKE_YEUKAI_4367_DONE', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, target_quantity=240, status='completed',
		)
		admin_url = '/admin/loraApi/stocktransfer/'
		post_data = {
			'action': 'delete_pending_yeukai_stocktakes',
			'index': '0',
			'_selected_action': [stocktake.pk, pending_transfer.pk, other_branch.pk, completed.pk],
		}

		confirmation = self.client.post(admin_url, post_data)
		self.assertEqual(confirmation.status_code, 200)
		self.assertContains(confirmation, 'Are you sure')

		post_data['post'] = 'yes'
		response = self.client.post(admin_url, post_data)
		self.assertEqual(response.status_code, 302)
		self.assertFalse(StockTransfer.objects.filter(pk=stocktake.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=pending_transfer.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=other_branch.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=completed.pk).exists())

	def test_admin_can_delete_selected_failed_yeukai_commands_only(self):
		failed_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_FAILED_TEST', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, status='failed',
		)
		failed_stocktake = StockTransfer.objects.create(
			transfer_id='STOCKTAKE_YEUKAI_FAILED_TEST', branch='YEUKAI', product_id=4075,
			product_name='HONEYPOT MAHEU', quantity=100, target_quantity=100, status='failed',
		)
		pending = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_PENDING_KEEP', branch='YEUKAI', product_id=4588,
			product_name='GAMBUKAMBU', quantity=60,
		)
		completed = StockTransfer.objects.create(
			transfer_id='TRANSFER_YEUKAI_COMPLETED_KEEP', branch='YEUKAI', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, status='completed',
		)
		other_branch = StockTransfer.objects.create(
			transfer_id='TRANSFER_OTHER_FAILED_KEEP', branch='BranchA', product_id=4367,
			product_name='CAPTAIN GINGER', quantity=240, status='failed',
		)
		admin_url = '/admin/loraApi/stocktransfer/'
		post_data = {
			'action': 'delete_failed_yeukai_commands',
			'index': '0',
			'_selected_action': [
				failed_transfer.pk, failed_stocktake.pk, pending.pk, completed.pk, other_branch.pk,
			],
		}

		confirmation = self.client.post(admin_url, post_data)
		self.assertEqual(confirmation.status_code, 200)
		self.assertContains(confirmation, 'Are you sure')

		post_data['post'] = 'yes'
		response = self.client.post(admin_url, post_data)
		self.assertEqual(response.status_code, 302)
		self.assertFalse(StockTransfer.objects.filter(pk=failed_transfer.pk).exists())
		self.assertFalse(StockTransfer.objects.filter(pk=failed_stocktake.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=pending.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=completed.pk).exists())
		self.assertTrue(StockTransfer.objects.filter(pk=other_branch.pk).exists())

	def test_direct_send_adds_quantity_to_main_and_branch(self):
		MainStockBalance.objects.filter(product_id=999).update(quantity=150)
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 100}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 250)
		self.assertFalse(ProductCatalog.objects.filter(branch='BranchA', product_id=999).exists())
		self.assertEqual(StockTransfer.objects.get().quantity, 100)

	def test_transfer_adds_to_main_and_branch_and_marks_branch_target_pending(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 5}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 10)
		transfer = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 15)
		self.assertEqual(catalog.available_quantity, 15)
		self.assertTrue(catalog.pending_stock_adjustment)
		self.assertEqual(catalog.pending_stock_quantity, 15)

	def test_transfer_is_counted_once_in_web_movement_balance(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=10, source='opening stock',
		)
		response = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 10}),
			content_type='application/json',
		)
		transfer = StockTransfer.objects.get()

		self.assertEqual(response.status_code, 202)
		self.assertEqual(StockMovement.objects.filter(product_id=999, branch='BranchA').count(), 1)
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		product = self.client.get('/api/stock/summary/?branch=BranchA').json()['products'][0]
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 20)
		self.assertEqual(StockMovement.objects.filter(product_id=999, branch='BranchA').count(), 2)
		self.assertEqual(product['movement_balance'], '20')

	def test_movement_page_shows_who_sent_the_transfer(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=999, product_name='Test Product', available_quantity=0)
		self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'quantity': 5}),
			content_type='application/json',
		)
		transfer = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		movements = self.client.get('/api/stock/movements/history/?branch=BranchA&product_id=999').json()['movements']
		self.assertEqual([(item['movement_type'], item['source']) for item in movements], [
			('received', 'transfer-admin (transfer to BranchA)'),
		])

	def test_old_transfer_movements_get_the_user_who_sent_them(self):
		from django.apps import apps
		migration = importlib.import_module('loraApi.migrations.0044_transfer_movement_done_by')
		now = timezone.now()
		StockTransfer.objects.create(
			transfer_id='t-old', branch='BranchA', product_id=999, product_name='Test Product', quantity=5,
			status='completed', created_by='Anesu', completed_at=now,
		)
		old = StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', movement_type='received', quantity=5,
			source='Web stock transfer',
		)
		unmatched = StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', movement_type='received', quantity=7,
			source='Web stock transfer',
		)
		migration.name_transfer_movements(apps, None)
		old.refresh_from_db()
		unmatched.refresh_from_db()
		self.assertEqual(old.source, 'Anesu (transfer to BranchA)')
		self.assertEqual(unmatched.source, 'Web stock transfer')

	def test_stock_take_updates_main_and_branch_together(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=4,
		)
		response = self.client.post(
			'/api/stock/main/adjust/',
			data=json.dumps({'product_id': 999, 'product_name': 'Test Product', 'quantity': 12, 'branch': 'BranchA'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(MainStockBalance.objects.get(product_id=999).quantity, 12)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 4)
		stock_take = StockTransfer.objects.get()
		self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': stock_take.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 12)
		self.assertFalse(StockMovement.objects.filter(product_id=999, movement_type='sold').exists())

	def test_repeated_success_acknowledgment_is_idempotent(self):
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_TEST', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)
		payload = json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True})

		first = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')
		completed_at = StockTransfer.objects.get().completed_at
		second = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(StockTransfer.objects.get().status, 'completed')
		self.assertEqual(StockTransfer.objects.get().completed_at, completed_at)

	def test_successful_transfer_updates_branch_available_stock_once(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_AVAILABLE', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)
		payload = json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True})

		first = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')
		second = self.client.post('/api/stock/transfers/complete/', data=payload, content_type='application/json')

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=999).available_quantity, 15)

	def test_stock_summary_server_cursor_includes_completed_transfer_quantity(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		initial_summary = self.client.get('/api/stock/summary/?branch=BranchA').json()
		self.assertIn('as_of', initial_summary)
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_CURSOR', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)

		completion = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		updated_summary = self.client.get(
			f"/api/stock/summary/?branch=BranchA&since={initial_summary['as_of']}"
		).json()

		self.assertEqual(completion.status_code, 200)
		self.assertTrue(updated_summary['changed'])
		self.assertEqual(updated_summary['products'][0]['available_quantity'], '15')

	def test_transfer_completion_merges_case_variant_catalog_rows(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1001, product_name='Test Product', available_quantity=10,
		)
		ProductCatalog.objects.create(
			branch='brancha', product_id=1001, product_name='Test Product', available_quantity=20,
		)
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_1001_DUPLICATE', branch='BranchA', product_id=1001,
			product_name='Test Product', quantity=5,
		)

		response = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(ProductCatalog.objects.filter(branch__iexact='BranchA', product_id=1001).count(), 1)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=1001).available_quantity, 15)

	def test_transfer_completion_updates_movement_balance_without_snapshot_double_count(self):
		movement_date = timezone.localdate()
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
			pos_movement_date=movement_date, pos_sold_quantity=0, pos_received_quantity=2,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=2, source='POS receiving',
		)
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_MOVEMENT', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)

		completion = self.client.post(
			'/api/stock/transfers/complete/',
			data=json.dumps({'transfer_id': transfer.transfer_id, 'branch': 'BranchA', 'success': True}),
			content_type='application/json',
		)
		catalog = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		self.assertEqual(completion.status_code, 200)
		self.assertEqual(catalog.available_quantity, 15)
		self.assertEqual(catalog.pos_received_quantity, 7)
		self.assertEqual(
			StockMovement.objects.filter(branch='BranchA', product_id=999, movement_type='received').count(),
			2,
		)

		snapshot = self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({
				'branch': 'BranchA',
				'movement_date': movement_date.isoformat(),
				'products': [{
					'product_id': 999, 'product_name': 'Test Product', 'available_quantity': 15,
					'sold_quantity': 0, 'received_quantity': 7,
				}],
			}),
			content_type='application/json',
		)
		self.assertEqual(snapshot.status_code, 200)
		self.assertEqual(
			StockMovement.objects.filter(branch='BranchA', product_id=999, movement_type='received').count(),
			2,
		)
		stock_summary = self.client.get('/api/stock/summary/?branch=BranchA').json()
		self.assertEqual(stock_summary['products'][0]['movement_balance'], '7')

	def test_branch_price_is_queued_for_all_confirmed_branches(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		ProductCatalog.objects.create(
			branch='BranchB', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		ProductCatalog.objects.create(
			branch='BranchPending', product_id=999, product_name='Test Product', selling_price='10.00',
			branch_confirmed=False,
		)
		ProductCatalog.objects.create(
			branch='MAIN', product_id=999, product_name='Test Product', selling_price='10.00',
		)
		queued = self.client.post(
			'/api/stock/prices/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'product_name': 'Test Product', 'selling_price': '12.50'}),
			content_type='application/json',
		)

		self.assertEqual(queued.status_code, 202)
		self.assertEqual(queued.json()['branch_count'], 2)
		self.assertCountEqual(queued.json()['branches'], ['BranchA', 'BranchB'])
		for branch in ['BranchA', 'BranchB']:
			catalog = ProductCatalog.objects.get(branch=branch, product_id=999)
			self.assertEqual(catalog.selling_price, 10)
			self.assertTrue(catalog.pending_price_update)
			self.assertEqual(catalog.pending_selling_price, 12.5)

		confirmed = self.client.post(
			'/api/stock/prices/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 999, 'success': True}),
			content_type='application/json',
		)

		self.assertEqual(confirmed.status_code, 200)
		branch_a = ProductCatalog.objects.get(branch='BranchA', product_id=999)
		branch_b = ProductCatalog.objects.get(branch='BranchB', product_id=999)
		self.assertEqual(branch_a.selling_price, 12.5)
		self.assertFalse(branch_a.pending_price_update)
		self.assertEqual(branch_b.selling_price, 10)
		self.assertTrue(branch_b.pending_price_update)
		self.assertFalse(ProductCatalog.objects.get(branch='BranchPending', product_id=999).pending_price_update)
		self.assertFalse(ProductCatalog.objects.get(branch='MAIN', product_id=999).pending_price_update)

	def test_stock_summary_counts_queued_transfer_as_sent(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=0,
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_BRANCHA_999_QUEUED', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=5,
		)

		response = self.client.get('/api/stock/summary/?branch=brancha')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['products'][0]['sent_quantity'], '5')

	def test_stock_transfer_log_filters_by_creation_date(self):
		transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_DATE_FILTER', branch='BranchA', product_id=1001,
			product_name='Date Filter Product', quantity=3,
		)
		selected_date = timezone.localdate() - timedelta(days=1)
		created_at = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
		StockTransfer.objects.filter(pk=transfer.pk).update(created_at=created_at)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_DATE_OTHER', branch='BranchA', product_id=1002,
			product_name='Other Date Product', quantity=2,
		)

		response = self.client.get('/api/stock/transfers/log/', {'date': selected_date.isoformat()})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['date'], selected_date.isoformat())
		self.assertEqual([item['transfer_id'] for item in response.json()['transfers']], ['TRANSFER_DATE_FILTER'])

	def test_stock_transfer_log_filters_by_branch_and_datetime_range(self):
		start = timezone.now() - timedelta(days=1)
		end = start + timedelta(hours=2)
		in_range = StockTransfer.objects.create(
			transfer_id='TRANSFER_RANGE_MATCH', branch='BranchA', product_id=1001,
			product_name='Range Match Product', quantity=3,
		)
		wrong_branch = StockTransfer.objects.create(
			transfer_id='TRANSFER_RANGE_BRANCH', branch='BranchB', product_id=1002,
			product_name='Wrong Branch Product', quantity=2,
		)
		out_of_range = StockTransfer.objects.create(
			transfer_id='TRANSFER_RANGE_TIME', branch='BranchA', product_id=1003,
			product_name='Outside Range Product', quantity=1,
		)
		StockTransfer.objects.filter(pk=in_range.pk).update(created_at=start + timedelta(minutes=30))
		StockTransfer.objects.filter(pk=wrong_branch.pk).update(created_at=start + timedelta(minutes=45))
		StockTransfer.objects.filter(pk=out_of_range.pk).update(created_at=end + timedelta(minutes=1))

		response = self.client.get('/api/stock/transfers/log/', {
			'branch': 'brancha',
			'start_datetime': start.isoformat(),
			'end_datetime': end.isoformat(),
		})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			[item['transfer_id'] for item in response.json()['transfers']],
			['TRANSFER_RANGE_MATCH'],
		)

	def test_stock_transfer_log_searches_by_product_name_or_id(self):
		StockTransfer.objects.create(
			transfer_id='TRANSFER_SEARCH_NAME', branch='BranchA', product_id=2001,
			product_name='Green Tea', quantity=3,
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_SEARCH_ID', branch='BranchB', product_id=2002,
			product_name='Coffee Beans', quantity=2,
		)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_SEARCH_OTHER', branch='BranchA', product_id=3001,
			product_name='Rice', quantity=1,
		)

		by_name = self.client.get('/api/stock/transfers/log/', {'product': 'tea'})
		by_id = self.client.get('/api/stock/transfers/log/', {'product': '2002'})
		selected_product = self.client.get('/api/stock/transfers/log/', {'product_id': '2002'})

		self.assertEqual(
			[item['transfer_id'] for item in by_name.json()['transfers']],
			['TRANSFER_SEARCH_NAME'],
		)
		self.assertEqual(
			[item['transfer_id'] for item in by_id.json()['transfers']],
			['TRANSFER_SEARCH_ID'],
		)
		self.assertEqual(selected_product.status_code, 200)
		self.assertEqual(
			[item['transfer_id'] for item in selected_product.json()['transfers']],
			['TRANSFER_SEARCH_ID'],
		)

	def test_stock_transfer_log_rejects_incomplete_or_reversed_datetime_range(self):
		incomplete = self.client.get('/api/stock/transfers/log/', {'start_datetime': timezone.now().isoformat()})
		reversed_range = self.client.get('/api/stock/transfers/log/', {
			'start_datetime': (timezone.now() + timedelta(hours=1)).isoformat(),
			'end_datetime': timezone.now().isoformat(),
		})

		self.assertEqual(incomplete.status_code, 400)
		self.assertEqual(reversed_range.status_code, 400)

	def test_stock_transfer_history_page_shows_branch_and_time_filters(self):
		StockTransfer.objects.create(
			transfer_id='TRANSFER_HISTORY_PAGE', branch='Offline Branch', product_id=1001,
			product_name='History Page Product', quantity=1,
		)

		response = self.client.get('/stock/transfers/history/')

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Stock transfer history')
		self.assertContains(response, 'Offline Branch')
		self.assertContains(response, 'From date &amp; time')
		self.assertContains(response, 'To date &amp; time')
		self.assertContains(response, 'Product search')
		self.assertContains(response, 'Type to find and select a product')
		self.assertContains(response, 'product-options')

	def test_stock_summary_returns_only_changed_products(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1001, product_name='Test Product')
		ProductCatalog.objects.filter(product_id=1001).update(updated_at=timezone.now() - timedelta(minutes=10))
		no_changes = self.client.get('/api/stock/summary/', {
			'branch': 'BranchA',
			'since': (timezone.now() - timedelta(minutes=1)).isoformat(),
		})
		self.assertFalse(no_changes.json()['changed'])
		self.assertEqual(no_changes.json()['products'], [])

		change_start = timezone.now()
		product = ProductCatalog.objects.get(product_id=1001)
		product.available_quantity = 7
		product.save()
		changed = self.client.get('/api/stock/summary/', {
			'branch': 'BranchA',
			'since': change_start.isoformat(),
		})
		self.assertFalse(changed.json()['full'])
		self.assertEqual([item['product_id'] for item in changed.json()['products']], [1001])

	def test_product_sync_inbox_returns_only_changed_products(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=1101, product_name='Test Product')
		ProductCatalog.objects.filter(product_id=1101).update(updated_at=timezone.now() - timedelta(minutes=10))
		no_changes = self.client.get('/api/products/inbox/', {
			'since': (timezone.now() - timedelta(minutes=1)).isoformat(),
		})
		self.assertFalse(no_changes.json()['changed'])
		self.assertEqual(no_changes.json()['products'], [])

		change_start = timezone.now()
		product = ProductCatalog.objects.get(product_id=1101)
		product.available_quantity = 8
		product.save()
		changed = self.client.get('/api/products/inbox/', {'since': change_start.isoformat()})
		self.assertFalse(changed.json()['full'])
		self.assertEqual([item['product_id'] for item in changed.json()['products']], [1101])

	def test_shared_product_catalog_exposes_products_for_branch_sync(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1101, product_name='Branch Product',
			product_code='B-1101', barcode='111', selling_price='8.00', tax_rate='5.00',
		)
		ProductCatalog.objects.create(
			branch='MAIN', product_id=1101, product_name='Canonical Product',
			product_code='M-1101', barcode='222', selling_price='10.00', tax_rate='7.50',
		)

		response = self.client.get('/api/products/shared/')

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()['full'])
		self.assertEqual(response.json()['products'], [{
			'product_id': 1101,
			'product_name': 'Canonical Product',
			'product_code': 'M-1101',
			'barcode': '222',
			'selling_price': '10.00',
			'tax_rate': '7.50',
		}])

	def test_branch_price_update_refreshes_shared_catalog_price(self):
		for branch in ('BranchA', 'BranchB', 'MAIN'):
			ProductCatalog.objects.create(
				branch=branch, product_id=1201, product_name='Priced Product', selling_price='10.00',
			)

		queued = self.client.post(
			'/api/stock/prices/',
			data=json.dumps({
				'branch': 'BranchA', 'product_id': 1201,
				'product_name': 'Priced Product', 'selling_price': '12.50',
			}),
			content_type='application/json',
		)

		self.assertEqual(queued.status_code, 202)
		self.assertEqual(str(ProductCatalog.objects.get(branch='MAIN', product_id=1201).selling_price), '12.50')
		shared = self.client.get('/api/products/shared/')
		self.assertEqual(shared.status_code, 200)
		product = next(item for item in shared.json()['products'] if item['product_id'] == 1201)
		self.assertEqual(product['selling_price'], '12.50')

	def test_branch_list_excludes_catalog_branch_without_heartbeat(self):
		ProductCatalog.objects.create(
			branch='Offline Branch', product_id=1000, product_name='Offline Product',
		)

		response = self.client.get('/api/branches/')

		self.assertEqual(response.status_code, 200)
		self.assertNotIn('Offline Branch', [item['name'] for item in response.json()['branches']])

	def test_branch_list_accepts_only_online_branch_pc_heartbeats(self):
		main_response = self.client.post(
			'/api/branches/',
			data=json.dumps({'branch': 'CloudPOS', 'device_role': 'Main PC'}),
			content_type='application/json',
		)
		branch_response = self.client.post(
			'/api/branches/',
			data=json.dumps({'branch': 'Online Branch', 'device_role': 'Branch PC'}),
			content_type='application/json',
		)

		self.assertEqual(main_response.status_code, 400)
		self.assertEqual(branch_response.status_code, 200)
		branches = self.client.get('/api/branches/').json()['branches']
		self.assertEqual([item['name'] for item in branches], ['Online Branch'])

	def test_branch_list_supports_twelve_online_branch_pc_heartbeats(self):
		for index in range(1, 13):
			response = self.client.post(
				'/api/branches/',
				data=json.dumps({'branch': f'Branch {index}', 'device_role': 'Branch PC'}),
				content_type='application/json',
			)
			self.assertEqual(response.status_code, 200)

		branches = self.client.get('/api/branches/').json()['branches']
		self.assertEqual(len(branches), 12)

	def test_publish_product_catalog_upserts_products(self):
		response = self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchA', 'product_id': 1002, 'product_name': 'Published Product',
				'available_quantity': 8, 'selling_price': '12.50', 'tax_rate': '5',
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['updated'], 1)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=1002)
		self.assertEqual(product.product_name, 'Published Product')
		self.assertEqual(product.available_quantity, 8)
		self.assertEqual(str(product.tax_rate), '5.00')

	def test_publish_product_catalog_keeps_existing_tax_rate_when_payload_omits_it(self):
		ProductCatalog.objects.create(
			branch='MAIN', product_id=1003, product_name='Published Product Existing Tax',
			selling_price='12.50', tax_rate='12.50', branch_confirmed=True,
		)
		response = self.client.post(
			'/api/products/publish/',
			data=json.dumps({'products': [{
				'branch': 'BranchA', 'product_id': 1003, 'product_name': 'Published Product Existing Tax',
				'available_quantity': 8, 'selling_price': '12.50',
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=1003).tax_rate), '12.50')

	def test_web_can_queue_branch_product_and_branch_can_acknowledge_it(self):
		BranchHeartbeat.objects.create(branch='BranchB', last_seen=timezone.now())
		BranchHeartbeat.objects.create(branch='Offline Branch', last_seen=timezone.now() - timedelta(days=1))
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({
				'branch': 'BranchA', 'product_name': 'New Branch Product',
				'product_id': '12100001', 'product_code': 'NEW-001', 'barcode': '990001', 'initial_quantity': '8', 'selling_price': '4.25', 'tax_rate': '7.50',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(set(response.json()['branches']), {'BranchA', 'BranchB', 'Offline Branch'})
		product_id = response.json()['product_id']
		self.assertEqual(product_id, 12_100_001)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).product_code, '12100002')
		self.assertTrue(ProductCatalog.objects.get(branch='BranchB', product_id=product_id).pending_product_creation)
		self.assertTrue(ProductCatalog.objects.get(branch='Offline Branch', product_id=product_id).pending_product_creation)
		self.assertFalse(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).branch_confirmed)
		self.assertEqual(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).pending_stock_quantity, 0)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=product_id).tax_rate), '7.50')
		not_visible = self.client.get('/api/products/?branch=BranchA&q=New%20Branch%20Product')
		self.assertEqual(not_visible.status_code, 200)
		self.assertEqual(not_visible.json()['products'], [])

		poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(poll.status_code, 200)
		self.assertEqual(poll.json()['pending_product_creations'][0]['product_name'], 'New Branch Product')
		self.assertEqual(poll.json()['pending_product_creations'][0]['initial_quantity'], '0')
		self.assertEqual(poll.json()['pending_product_creations'][0]['tax_rate'], '7.50')

		complete = self.client.post(
			'/api/products/create/complete/',
			data=json.dumps({'branch': 'BranchA', 'product_id': product_id, 'actual_product_id': 2001, 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(complete.status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=2001)
		self.assertFalse(product.pending_product_creation)
		self.assertTrue(product.branch_confirmed)
		self.assertEqual(str(product.tax_rate), '7.50')
		self.assertEqual(str(ProductCatalog.objects.get(branch='MAIN', product_id=2001).tax_rate), '7.50')
		visible = self.client.get('/api/products/?branch=BranchA&q=New%20Branch%20Product')
		self.assertEqual([item['product_id'] for item in visible.json()['products']], [2001])
		main_products = self.client.get('/api/products/?branch=MAIN&q=New%20Branch%20Product')
		self.assertEqual([item['product_id'] for item in main_products.json()['products']], [2001])
		transfer = self.client.post(
			'/api/stock/transfers/',
			data=json.dumps({'branch': 'BranchA', 'product_id': 2001, 'product_name': 'New Branch Product', 'quantity': 6}),
			content_type='application/json',
		)
		self.assertEqual(transfer.status_code, 202)
		self.assertEqual(MainStockBalance.objects.get(product_id=2001).quantity, 6)

	def test_web_rejects_main_as_branch_product_target(self):
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({'branch': 'MAIN', 'product_name': 'Invalid Product', 'selling_price': '1.00'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)

	def test_web_rejects_product_id_above_sql_integer_limit(self):
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({
				'branch': 'BranchA', 'product_id': '12100000324',
				'product_name': 'TEST FOR ALL BRANCHIES', 'product_code': '12100000325',
				'initial_quantity': '0', 'selling_price': '1',
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Product ID cannot exceed 2147483647.')
		self.assertFalse(ProductCatalog.objects.filter(product_id=12100000324).exists())

	def test_web_rejects_duplicate_product_id_with_clear_message(self):
		ProductCatalog.objects.create(branch='BranchA', product_id=12100002, product_name='Existing Product')
		response = self.client.post(
			'/api/products/create/',
			data=json.dumps({'branch': 'BranchB', 'product_id': 12100002, 'product_name': 'Duplicate Product', 'selling_price': '1.00'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 409)
		self.assertIn('already queued', response.json()['message'])

	def test_branch_sync_clears_pending_flags_for_all_branches_when_no_branch_filter_is_used(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=6001, product_name='Pending BranchA Product',
			product_code='6001', pending_product_creation=True, pending_stock_quantity=5,
			branch_confirmed=False,
		)
		ProductCatalog.objects.create(
			branch='BranchB', product_id=6002, product_name='Pending BranchB Product',
			product_code='6002', selling_price='10.00', pending_price_update=True,
			pending_selling_price='12.50', branch_confirmed=True,
		)

		response = self.client.get('/api/branch-sync/')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['pending_price_updates'], [])
		self.assertEqual(response.json()['pending_product_creations'], [])
		self.assertFalse(ProductCatalog.objects.filter(pending_product_creation=True).exists())
		self.assertFalse(ProductCatalog.objects.filter(pending_price_update=True).exists())

	def test_web_can_queue_and_branch_can_claim_invoice_reprint(self):
		response = self.client.post(
			'/api/invoice-reprint/',
			data=json.dumps({'branch': 'BranchA', 'invoice': 'INV-100'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		poll = self.client.get('/api/branch-sync/?branch=BranchA')
		self.assertEqual(poll.status_code, 200)
		self.assertEqual(poll.json()['pending_invoice_reprints'][0]['invoice'], 'INV-100')
		request_id = poll.json()['pending_invoice_reprints'][0]['request_id']
		complete = self.client.post(
			'/api/invoice-reprint/complete/',
			data=json.dumps({'request_id': request_id, 'success': True}),
			content_type='application/json',
		)
		self.assertEqual(complete.status_code, 200)
		self.assertEqual(InvoiceReprintRequest.objects.get(request_id=request_id).status, 'completed')

	def test_product_search_matches_code_barcode_and_id(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1001, product_name='Search Product',
			product_code='CODE-1001', barcode='BAR-1001',
		)

		for query in ('CODE-1001', 'BAR-1001', '1001'):
			response = self.client.get(f'/api/products/?branch=BranchA&q={query}')
			self.assertEqual(response.status_code, 200)
			self.assertEqual([item['product_id'] for item in response.json()['products']], [1001])

	def test_stock_summary_sold_and_received_are_daily_for_selected_branch(self):
		from django.utils import timezone
		from datetime import timedelta

		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10, sold_quantity=99,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=5, source='today',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=3, source='today',
		)
		old_movement = StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=40, source='yesterday',
		)
		StockMovement.objects.filter(pk=old_movement.pk).update(
			created_at=timezone.now() - timedelta(days=1),
		)
		StockMovement.objects.create(
			branch='BranchB', product_id=999, product_name='Test Product',
			movement_type='received', quantity=70, source='other branch',
		)

		response = self.client.get('/api/stock/summary/?branch=BranchA')

		self.assertEqual(response.status_code, 200)
		product = response.json()['products'][0]
		self.assertEqual(product['received_quantity'], '5')
		self.assertEqual(product['sold_quantity'], '3')

	def test_stock_summary_filters_sent_and_sold_by_selected_date(self):
		selected_date = timezone.localdate() - timedelta(days=1)
		ProductCatalog.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product', available_quantity=10,
		)
		old_sale = StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=4, source='historical sale',
		)
		selected_day_start = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
		StockMovement.objects.filter(pk=old_sale.pk).update(created_at=selected_day_start)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=2, source='today sale',
		)
		old_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_SELECTED_DAY', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=7,
		)
		StockTransfer.objects.filter(pk=old_transfer.pk).update(created_at=selected_day_start)
		StockTransfer.objects.create(
			transfer_id='TRANSFER_TODAY', branch='BranchA', product_id=999,
			product_name='Test Product', quantity=11,
		)

		response = self.client.get('/api/stock/summary/', {
			'branch': 'BranchA',
			'date': selected_date.isoformat(),
		})

		self.assertEqual(response.status_code, 200)
		product = response.json()['products'][0]
		self.assertEqual(product['sold_quantity'], '4')
		self.assertEqual(product['sent_quantity'], '7')

	def test_stock_summary_lists_the_movement_types_of_the_selected_day(self):
		selected_date = timezone.localdate() - timedelta(days=1)
		day_start = timezone.make_aware(datetime.combine(selected_date, datetime.min.time()))
		for product_id in (1201, 1202):
			ProductCatalog.objects.create(branch='BranchA', product_id=product_id, product_name=f'P{product_id}', available_quantity=5)
		for movement_type in ('maintained', 'sold', 'sold'):
			movement = StockMovement.objects.create(
				branch='BranchA', product_id=1201, product_name='P1201', movement_type=movement_type, quantity=1, source='test',
			)
			StockMovement.objects.filter(pk=movement.pk).update(created_at=day_start)
		StockMovement.objects.create(
			branch='BranchA', product_id=1202, product_name='P1202', movement_type='received', quantity=3, source='today',
		)

		response = self.client.get('/api/stock/summary/', {'branch': 'BranchA', 'date': selected_date.isoformat()})

		products = {item['product_id']: item for item in response.json()['products']}
		self.assertEqual(products[1201]['day_movements'], ['maintained', 'sold'])
		self.assertEqual(products[1202]['day_movements'], [])

	def test_stock_summary_shows_the_quantum_movement_balance_beside_the_branch_balance(self):
		snapshot = lambda **extra: self.client.post('/api/stock/snapshot/', data=json.dumps({
			'branch': 'BranchA',
			'products': [{'product_id': 5576, 'product_name': 'WILLARDS CORNFLAKES 500G', 'available_quantity': '12', **extra}],
		}), content_type='application/json')
		self.assertEqual(snapshot().status_code, 200)
		product = self.client.get('/api/stock/summary/', {'branch': 'BranchA'}).json()['products'][0]
		self.assertEqual(product['available_quantity'], '12')

		self.assertEqual(snapshot(movement_balance='-6.00000').status_code, 200)
		cache.clear()
		product = next(
			item for item in self.client.get('/api/stock/summary/', {'branch': 'BranchA'}).json()['products']
			if item['product_id'] == 5576
		)
		self.assertEqual(product['available_quantity'], '12')
		self.assertEqual(product['movement_balance'], '-6')

	def test_stock_summary_rejects_invalid_selected_date(self):
		response = self.client.get('/api/stock/summary/?branch=BranchA&date=not-a-date')
		self.assertEqual(response.status_code, 400)

	def test_stock_summary_reports_negative_movement_balance_for_branch_products(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1001, product_name='Negative movement product', available_quantity=4,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1001, product_name='Negative movement product',
			movement_type='received', quantity=5, source='opening stock',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1001, product_name='Negative movement product',
			movement_type='sold', quantity=8, source='sale',
		)

		response = self.client.get('/api/stock/summary/?branch=BranchA')

		self.assertEqual(response.status_code, 200)
		product = response.json()['products'][0]
		self.assertEqual(product['available_quantity'], '4')
		self.assertEqual(product['movement_balance'], '-3')

	def test_pos_stock_snapshot_updates_negative_balance_and_is_idempotent(self):
		from django.utils import timezone

		ProductCatalog.objects.create(
			branch='BranchA', product_id=1004, product_name='TEST PRODUCT',
			product_code='1005', available_quantity=300,
			pos_movement_date=timezone.localdate(), pos_sold_quantity=400, pos_received_quantity=500,
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1004, product_name='TEST PRODUCT',
			movement_type='adjusted', quantity=500, source='previous snapshot',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1004, product_name='TEST PRODUCT',
			movement_type='received', quantity=100, source='receipt',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1004, product_name='TEST PRODUCT',
			movement_type='sold', quantity=300, source='sale',
		)
		payload = {'branch': 'BranchA', 'products': [{
			'product_id': 1004, 'product_name': 'TEST PRODUCT', 'product_code': '1005',
			'available_quantity': -700, 'sold_quantity': 1011, 'received_quantity': 1200,
		}], 'movement_date': timezone.localdate().isoformat()}

		first_response = self.client.post(
			'/api/stock/snapshot/', data=json.dumps(payload), content_type='application/json',
		)
		second_response = self.client.post(
			'/api/stock/snapshot/', data=json.dumps(payload), content_type='application/json',
		)
		summary = self.client.get('/api/stock/summary/?branch=BranchA').json()

		self.assertEqual(first_response.status_code, 200)
		self.assertEqual(first_response.json()['movements_recorded'], 3)
		self.assertEqual(second_response.status_code, 200)
		self.assertEqual(second_response.json()['movements_recorded'], 0)
		self.assertEqual(StockMovement.objects.filter(
			branch='BranchA', product_id=1004, source='pos_stock_snapshot',
		).count(), 1)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1004, source='POS sales',
		).quantity, 611)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1004, source='POS receiving',
		).quantity, 700)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1004, source='pos_stock_snapshot',
		).movement_type, 'maintained')
		product = next(item for item in summary['products'] if item['product_id'] == 1004)
		self.assertEqual(product['available_quantity'], '-700')
		self.assertEqual(product['movement_balance'], '-700')
		self.assertEqual(product['sold_quantity'], '1011')
		self.assertEqual(product['received_quantity'], '1200')

	def test_pos_sales_delta_does_not_create_maintained_movement(self):
		from django.utils import timezone

		ProductCatalog.objects.create(
			branch='BranchA', product_id=1005, product_name='POS Sale Product',
			available_quantity=500, pos_movement_date=timezone.localdate(),
			pos_sold_quantity=300, pos_received_quantity=50,
		)
		payload = {'branch': 'BranchA', 'products': [{
			'product_id': 1005, 'product_name': 'POS Sale Product',
			'available_quantity': 400, 'sold_quantity': 400, 'received_quantity': 50,
		}], 'movement_date': timezone.localdate().isoformat()}

		response = self.client.post(
			'/api/stock/snapshot/', data=json.dumps(payload), content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['movements_recorded'], 1)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1005, source='POS sales',
		).quantity, 100)
		self.assertFalse(StockMovement.objects.filter(
			branch='BranchA', product_id=1005, movement_type='maintained',
		).exists())

	def test_first_pos_snapshot_handles_missing_movement_counters(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1010, product_name='First snapshot product',
			available_quantity=10,
		)
		response = self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': 1010, 'product_name': 'First snapshot product',
				'available_quantity': 9, 'sold_quantity': 3, 'received_quantity': 2,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1010, source='POS sales',
		).quantity, 3)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1010, source='POS receiving',
		).quantity, 2)

	def _post_snapshot_with_tax(self, product_id, tax_rate):
		return self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': product_id, 'product_name': 'Taxed POS Product',
				'available_quantity': 5, 'tax_rate': tax_rate,
			}]}),
			content_type='application/json',
		)

	def test_pos_stock_snapshot_fills_missing_web_tax_rate(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1020, product_name='Taxed POS Product', available_quantity=5,
		)
		response = self._post_snapshot_with_tax(1020, '15')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=1020).tax_rate), '15.00')

	def test_pos_stock_snapshot_sets_tax_rate_on_new_product(self):
		response = self._post_snapshot_with_tax(1021, '15.5')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=1021).tax_rate), '15.50')

	def test_pos_stock_snapshot_never_overwrites_web_tax_rate(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1022, product_name='Taxed POS Product',
			available_quantity=5, tax_rate='7.50',
		)
		response = self._post_snapshot_with_tax(1022, '15')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(str(ProductCatalog.objects.get(branch='BranchA', product_id=1022).tax_rate), '7.50')

	def test_pos_stock_snapshot_ignores_invalid_tax_rate_without_rejecting_sync(self):
		ProductCatalog.objects.create(
			branch='BranchA', product_id=1023, product_name='Taxed POS Product', available_quantity=1,
		)
		response = self._post_snapshot_with_tax(1023, 'not-a-number')

		self.assertEqual(response.status_code, 200)
		product = ProductCatalog.objects.get(branch='BranchA', product_id=1023)
		self.assertEqual(product.available_quantity, 5)
		self.assertEqual(str(product.tax_rate), '0.00')

	def test_pos_stock_snapshot_normalizes_signed_sales_counters(self):
		from django.utils import timezone

		ProductCatalog.objects.create(
			branch='BranchA', product_id=1006, product_name='Signed POS Sale Product',
			available_quantity=200, pos_movement_date=timezone.localdate(),
			pos_sold_quantity=-300, pos_received_quantity=0,
		)
		response = self.client.post(
			'/api/stock/snapshot/',
			data=json.dumps({'branch': 'BranchA', 'products': [{
				'product_id': 1006, 'product_name': 'Signed POS Sale Product',
				'available_quantity': 100, 'sold_quantity': -400, 'received_quantity': 0,
			}], 'movement_date': timezone.localdate().isoformat()}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(StockMovement.objects.get(
			branch='BranchA', product_id=1006, source='POS sales',
		).quantity, 100)
		self.assertEqual(ProductCatalog.objects.get(product_id=1006).pos_sold_quantity, 400)

	def test_stock_activity_includes_completed_transfers_without_duplicate_movements(self):
		from django.utils import timezone

		legacy_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_ACTIVITY_LEGACY', branch='BranchA', product_id=1007,
			product_name='Legacy transfer product', quantity=4, status='completed',
			created_by='admin', completed_at=timezone.now(),
		)
		paired_transfer = StockTransfer.objects.create(
			transfer_id='TRANSFER_ACTIVITY_PAIRED', branch='BranchA', product_id=1008,
			product_name='Paired transfer product', quantity=6, status='completed',
			created_by='admin', completed_at=timezone.now(),
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1008, product_name='Paired transfer product',
			movement_type='received', quantity=6,
			source='admin (transfer to BranchA)',
		)

		response = self.client.get('/api/stock/movements/', {
			'branch': 'BranchA', 'date': timezone.localdate().isoformat(),
		})
		movements = response.json()['movements']

		self.assertEqual(response.status_code, 200)
		self.assertEqual(sum(item['product_id'] == legacy_transfer.product_id for item in movements), 1)
		self.assertEqual(sum(item['product_id'] == paired_transfer.product_id for item in movements), 1)
		legacy_event = next(item for item in movements if item['product_id'] == legacy_transfer.product_id)
		self.assertEqual(legacy_event['source'], 'Stock transfer')

	def test_stock_activity_shows_sold_quantity_not_pos_stock_snapshot_balance(self):
		from django.utils import timezone

		StockMovement.objects.create(
			branch='BranchA', product_id=1009, product_name='Sold product',
			movement_type='maintained', quantity=20, source='pos_stock_snapshot',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1009, product_name='Sold product',
			movement_type='sold', quantity=2, source='POS sales',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1009, product_name='Sold product',
			movement_type='maintained', quantity=17, source='pos_stock_snapshot',
		)

		response = self.client.get('/api/stock/movements/', {
			'branch': 'BranchA', 'date': timezone.localdate().isoformat(),
		})
		movements = response.json()['movements']

		self.assertEqual(response.status_code, 200)
		inferred_sale = next(item for item in movements if item['source'] == 'POS stock sync')
		self.assertEqual(len(movements), 2)
		self.assertEqual(inferred_sale['movement_type'], 'sold')
		self.assertEqual(inferred_sale['quantity'], '1')

	def test_stock_page_highlights_products_with_negative_movement_balance(self):
		response = self.client.get('/stock/')

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'negative-stock')
		self.assertContains(response, 'Negative movement')

	def test_stock_movements_can_be_filtered_by_date(self):
		from django.utils import timezone
		from datetime import timedelta

		today = StockMovement.objects.create(
			branch='BranchA', product_id=1001, product_name='Today Product',
			movement_type='received', quantity=1, source='sync',
		)
		yesterday = StockMovement.objects.create(
			branch='BranchA', product_id=1002, product_name='Yesterday Product',
			movement_type='received', quantity=2, source='sync',
		)
		StockMovement.objects.filter(pk=yesterday.pk).update(
			created_at=timezone.now() - timedelta(days=1),
		)

		response = self.client.get(f'/api/stock/movements/?branch=BranchA&date={timezone.localdate().isoformat()}')

		self.assertEqual(response.status_code, 200)
		self.assertEqual([movement['product_id'] for movement in response.json()['movements']], [today.product_id])

	def test_stock_movements_reject_invalid_timezone_offset(self):
		response = self.client.get('/api/stock/movements/?branch=BranchA&date=2026-09-25&timezone_offset=900')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Timezone offset is out of range.')

	def test_stock_movements_returns_only_twenty_newest_rows(self):
		for product_id in range(1, 26):
			StockMovement.objects.create(
				branch='BranchA', product_id=product_id, product_name=f'Product {product_id}',
				movement_type='received', quantity=1, source='sync',
			)

		response = self.client.get('/api/stock/movements/?branch=BranchA')

		self.assertEqual(response.status_code, 200)
		movements = response.json()['movements']
		self.assertEqual(len(movements), 20)

	def test_product_catalog_sync_is_disabled_and_preserves_case_variant_rows(self):
		ProductCatalog.objects.create(branch='YEUKAI', product_id=3979, available_quantity=10)
		ProductCatalog.objects.create(branch='yeukai', product_id=3979, available_quantity=12)

		response = self.client.post(
			'/api/products/sync/',
			data=json.dumps({'branch': 'YEUKAI', 'products': [{
				'product_id': 3979, 'product_name': 'Test Product', 'available_quantity': 7,
			}]}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 410)
		self.assertEqual(
			list(ProductCatalog.objects.filter(product_id=3979).order_by('branch').values_list('branch', 'available_quantity')),
			[('YEUKAI', 10), ('yeukai', 12)],
		)

	def test_product_movement_history_filters_product_and_date(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=12, source='transfer-admin',
		)

		response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.json()['movements']), 1)
		self.assertEqual(response.json()['movements'][0]['source'], 'transfer-admin')
		self.assertEqual(response.json()['movements'][0]['balance'], '12')

		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='sold', quantity=2, source='HP EliteBook',
		)
		sold_response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')
		sold_movement = next(item for item in sold_response.json()['movements'] if item['movement_type'] == 'sold')
		self.assertEqual(sold_movement['source'], 'Sold')

	def test_product_movement_history_filters_exact_product_id(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Duplicate Product',
			movement_type='received', quantity=4, source='selected product',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=1000, product_name='Duplicate Product',
			movement_type='received', quantity=9, source='same name different product',
		)

		response = self.client.get('/api/stock/movements/history/', {
			'branch': 'BranchA',
			'product_id': 999,
		})

		self.assertEqual(response.status_code, 200)
		self.assertEqual([item['product_id'] for item in response.json()['movements']], [999])

	def test_product_movement_history_rejects_invalid_date_range(self):
		response = self.client.get('/api/stock/movements/history/', {
			'branch': 'BranchA',
			'product_id': 999,
			'from': '2026-10-20',
			'to': '2026-10-01',
		})
		self.assertEqual(response.status_code, 400)

	def test_product_movement_history_adjustment_sets_balance(self):
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=12, source='transfer-admin',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='adjusted', quantity=5, source='Admin (stock take)',
		)
		StockMovement.objects.create(
			branch='BranchA', product_id=999, product_name='Test Product',
			movement_type='received', quantity=3, source='transfer-admin',
		)

		response = self.client.get('/api/stock/movements/history/?branch=BranchA&product=999&from=2020-01-01&to=2099-12-31')

		self.assertEqual(response.status_code, 200)
		self.assertEqual([item['balance'] for item in response.json()['movements']], ['8', '5', '12'])

	def test_product_movement_history_requires_branch(self):
		response = self.client.get('/api/stock/movements/history/')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Select a branch first.')

	def test_product_movement_history_requires_product(self):
		response = self.client.get('/api/stock/movements/history/?branch=BranchA')

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['message'], 'Select a product first.')


class DeletionQueueTests(TestCase):
	def setUp(self):
		self.client.force_login(get_user_model().objects.create_user(
			username='operator', password='OperatorPass4182!'
		))

	def test_dashboard_counts_saved_pending_record(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-100', 'branch': 'Branch A'}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 202)
		self.assertEqual(self.client.get('/api/main-sync/').json()['pending_count'], 1)

	def test_dashboard_and_queue_are_not_cached(self):
		self.assertEqual(self.client.get('/').headers['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0')
		self.assertEqual(self.client.get('/api/main-sync/').headers['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0')

	def test_confirmation_moves_record_to_processed_count(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-200', 'branch': 'Branch A'}),
			content_type='application/json',
		)
		deletion_id = response.json()['deletion_id']

		response = self.client.post(
			'/api/confirm-deletion/',
			data=json.dumps({
				'deletion_id': deletion_id,
				'deleted_rows': 1,
				'branch': 'Branch A',
				'success': True,
			}),
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 200)
		summary = self.client.get('/api/main-sync/').json()
		self.assertEqual(summary['pending_count'], 0)
		self.assertEqual(summary['processed_count'], 1)

	def test_pos_approval_keeps_web_cancellation_processing(self):
		deletion_id = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-APPROVAL', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()['deletion_id']

		response = self.client.post(
			'/api/confirm-deletion/',
			data=json.dumps({
				'deletion_id': deletion_id,
				'deleted_rows': 1,
				'branch': 'Branch A',
				'approval_pending': True,
			}),
			content_type='application/json',
		)

		self.assertEqual(response.json()['status'], 'awaiting_approval')
		self.assertEqual(self.client.get('/api/cancellation-history/?invoice=INV-APPROVAL').json()['cancellations'][0]['status'], 'processing')

	def test_main_queue_includes_cancellation_and_sales_report_statuses(self):
		cancellation = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-QUEUE', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()
		report = self.client.post(
			'/api/sales-report/',
			data=json.dumps({'report_date': '2026-09-25', 'branches': ['Branch A']}),
			content_type='application/json',
		).json()

		queue = self.client.get('/api/main-sync/').json()['queue']
		statuses = {(item['type'], item['id']): item['status'] for item in queue}
		self.assertEqual(statuses[('cancellation', cancellation['deletion_id'])], 'pending')
		self.assertEqual(statuses[('sales_report', report['reports'][0]['id'])], 'pending')

	def test_queue_shows_sent_and_printed_states(self):
		cancellation_id = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-SENT', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()['deletion_id']
		report_id = self.client.post(
			'/api/sales-report/',
			data=json.dumps({'report_date': '2026-09-25', 'branches': ['Branch A']}),
			content_type='application/json',
		).json()['reports'][0]['id']

		self.client.get('/api/branch-sync/?branch=Branch A')
		self.client.post(
			'/api/sales-report/complete/',
			data=json.dumps({'report_id': report_id, 'success': True, 'row_count': 4, 'branch': 'Branch A'}),
			content_type='application/json',
		)

		queue = self.client.get('/api/main-sync/').json()['queue']
		statuses = {item['id']: item['status'] for item in queue}
		self.assertEqual(statuses[cancellation_id], 'processing')
		self.assertEqual(statuses[report_id], 'printed')

	def test_scheduled_report_is_only_released_to_selected_branch_when_due(self):
		scheduled_at = timezone.now() + timedelta(minutes=2)
		response = self.client.post(
			'/api/sales-report/',
			data=json.dumps({
				'report_date': '2026-09-25',
				'branches': ['Branch A'],
				'scheduled_at': scheduled_at.isoformat(),
			}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 202)
		report_id = response.json()['reports'][0]['id']
		self.assertEqual(SalesReportRequest.objects.get(pk=report_id).status, 'scheduled')
		self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports'], [])

		SalesReportRequest.objects.filter(pk=report_id).update(scheduled_at=timezone.now() - timedelta(seconds=1))
		self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch B').json()['pending_reports'], [])
		branch_a_reports = self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports']
		self.assertEqual([report['id'] for report in branch_a_reports], [report_id])

	def test_daily_report_schedules_are_branch_specific_and_repeat_once_per_day(self):
		fixed_now = datetime(2026, 9, 26, 12, 1, tzinfo=datetime_timezone.utc)
		response = self.client.post(
			'/api/sales-report/schedules/',
			data=json.dumps({
				'schedules': [
					{'branch': 'Branch A', 'time': '12:00', 'timezone': 'UTC'},
					{'branch': 'Branch B', 'time': '12:02', 'timezone': 'UTC'},
				],
			}),
			content_type='application/json',
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(SalesReportSchedule.objects.count(), 2)

		with patch('loraApi.views.timezone.now', return_value=fixed_now):
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			daily_reports = [item for item in main_queue if item['type'] == 'sales_report']
			self.assertEqual(len(daily_reports), 1)
			self.assertEqual(daily_reports[0]['branch'], 'Branch A')
			branch_b_reports = self.client.get('/api/branch-sync/?branch=Branch B').json()['pending_reports']
			self.assertEqual(branch_b_reports, [])
			branch_a_reports = self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports']
			self.assertEqual(len(branch_a_reports), 1)
			self.assertEqual(branch_a_reports[0]['report_date'], '2026-09-26')
			self.assertEqual(self.client.get('/api/branch-sync/?branch=Branch A').json()['pending_reports'], [])
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			daily_reports = [item for item in main_queue if item['type'] == 'sales_report']
			self.assertEqual(len(daily_reports), 1)
			self.assertEqual(SalesReportRequest.objects.filter(branch='Branch A', report_date=fixed_now.date()).count(), 1)

		with patch('loraApi.views.timezone.now', return_value=fixed_now + timedelta(days=1)):
			main_queue = self.client.get('/api/main-sync/').json()['queue']
			next_day_reports = [
				item for item in main_queue
				if item['type'] == 'sales_report' and item['branch'] == 'Branch A' and item['detail'] == '2026-09-27'
			]
		self.assertEqual(len(next_day_reports), 1)
		self.assertEqual(next_day_reports[0]['detail'], '2026-09-27')

	def test_history_returns_confirmed_invoices_newest_first(self):
		older = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-OLD', 'branch': 'Branch A'}),
			content_type='application/json',
		).json()['deletion_id']
		newer = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-NEW', 'branch': 'Branch B'}),
			content_type='application/json',
		).json()['deletion_id']

		for deletion_id, branch in [(older, 'Branch A'), (newer, 'Branch B')]:
			self.client.post(
				'/api/confirm-deletion/',
				data=json.dumps({'deletion_id': deletion_id, 'deleted_rows': 1, 'branch': branch}),
				content_type='application/json',
			)

		response = self.client.get('/api/cancellation-history/?branch=Branch')
		self.assertEqual(response.status_code, 200)
		self.assertEqual([item['invoice'] for item in response.json()['cancellations']], ['INV-NEW', 'INV-OLD'])

	def test_web_cancellation_appears_in_history_while_pending(self):
		self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-WEB-PENDING', 'branch': 'Branch A'}),
			content_type='application/json',
		)

		response = self.client.get('/api/cancellation-history/?invoice=INV-WEB-PENDING')
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['count'], 1)
		self.assertEqual(response.json()['cancellations'][0]['status'], 'pending')

	def test_history_filters_by_invoice_and_returns_deleted_by(self):
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-FILTER', 'branch': 'Branch C'}),
			content_type='application/json',
		)
		deletion_id = response.json()['deletion_id']
		self.client.post(
			'/api/confirm-deletion/',
			data=json.dumps({
				'deletion_id': deletion_id,
				'deleted_rows': 2,
				'branch': 'Branch C',
				'deleted_by': 'cashier-17',
			}),
			content_type='application/json',
		)

		response = self.client.get('/api/cancellation-history/?invoice=FILTER')
		self.assertEqual(response.json()['cancellations'][0]['deleted_by'], 'operator')


class AuthenticationTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(
			username='Admin', password='Tash1nga4182', email='admin@example.com'
		)

	def test_dashboard_requires_login(self):
		response = self.client.get('/')
		self.assertRedirects(response, '/login/?next=/')

	def test_admin_can_create_user_and_change_password(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'operator',
			'password1': 'OperatorPass4182!',
			'password2': 'OperatorPass4182!',
		})
		self.assertEqual(response.status_code, 200)
		operator = get_user_model().objects.get(username='operator')
		self.assertFalse(operator.is_superuser)

		response = self.client.post('/users/', {
			'action': 'change_password',
			'user_id': operator.pk,
			'new_password1': 'ChangedPass4182!',
			'new_password2': 'ChangedPass4182!',
		})
		self.assertEqual(response.status_code, 200)
		self.assertTrue(operator.__class__.objects.get(pk=operator.pk).check_password('ChangedPass4182!'))

		response = self.client.post('/users/', {'action': 'delete', 'user_id': operator.pk})
		self.assertEqual(response.status_code, 200)
		self.assertFalse(get_user_model().objects.filter(username='operator').exists())

	def test_web_cancellation_records_logged_in_username(self):
		self.client.force_login(self.admin)
		response = self.client.post(
			'/api/cancel-sale/',
			data=json.dumps({'invoice': 'INV-ACTOR', 'branch': 'Branch A'}),
			content_type='application/json',
		)
		from loraApi.models import DeletionRecord
		record = DeletionRecord.objects.get(deletion_id=response.json()['deletion_id'])
		self.assertEqual(record.deleted_by, 'Admin')

	def test_invalid_user_creation_shows_validation_reason(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'Admin',
			'password1': 'short',
			'password2': 'short',
		})
		self.assertContains(response, 'already exists')

	def test_state_restore_keeps_admin_credentials_and_admin_role(self):
		self.admin.is_superuser = False
		self.admin.is_staff = True
		self.admin.save(update_fields=['is_superuser', 'is_staff'])

		call_command('restore_seed_state')

		admin = get_user_model().objects.get(username='Admin')
		self.assertTrue(admin.check_password('@dm1n4182'))
		self.assertTrue(admin.is_superuser)
		self.assertIsNotNone(authenticate(username='Admin', password='@dm1n4182'))

	def test_admin_can_create_user_and_persist_to_state_file(self):
		self.client.force_login(self.admin)
		response = self.client.post('/users/', {
			'action': 'create',
			'username': 'cashier18',
			'password1': 'Cashier@Pass4182!',
			'password2': 'Cashier@Pass4182!',
		})
		self.assertEqual(response.status_code, 200)
		state = load_state()
		self.assertIn('cashier18', [entry['username'] for entry in state['users']])
		self.assertTrue(get_user_model().objects.get(username='cashier18').check_password('Cashier@Pass4182!'))

	def test_logout_requires_post_and_ends_session(self):
		self.client.force_login(self.admin)
		self.assertEqual(self.client.post('/logout/').status_code, 302)
		self.assertEqual(self.client.post('/logout/').url, '/login/')
		self.assertRedirects(self.client.get('/'), '/login/?next=/')


class BranchRestoreTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser(username='restore-admin', password='RestorePass4182!')
		self.client.force_login(self.admin)
		ProductCatalog.objects.create(branch='WEST', product_id=701, product_name="KELLOGG'S", product_code='702', barcode='600', selling_price='2.50', tax_rate='15.00', available_quantity=24)
		ProductCatalog.objects.create(branch='WEST', product_id=703, product_name='Old Name', pending_product_name='Website Name', product_code='', selling_price='1.00', pending_price_update=True, pending_selling_price='1.20', available_quantity=3)
		ProductCatalog.objects.create(branch='WEST', product_id=705, product_name='Deleted Product', available_quantity=9)
		ProductCatalog.objects.create(branch='WEST', product_id=707, product_name='Still Being Created', pending_product_creation=True, available_quantity=0)
		ProductCatalog.objects.create(branch='VEES', product_id=701, product_name="KELLOGG'S", available_quantity=5)
		ProductCatalog.objects.create(branch='MAIN', product_id=701, product_name="KELLOGG'S", available_quantity=100)
		ProductDeletionRequest.objects.create(branch='WEST', product_id=705, status='completed')
		BranchHeartbeat.objects.create(branch='WEST', last_seen=timezone.now())

	def test_page_and_branch_list(self):
		page = self.client.get('/stock/restore/')
		self.assertContains(page, 'Branch restore')
		self.assertContains(page, 'Download restore script')
		self.assertContains(page, '/api/stock/restore/main-fallback.sql')

		branches = {item['branch']: item for item in self.client.get('/api/stock/restore/').json()['branches']}
		self.assertEqual(set(branches), {'VEES', 'WEST'})
		self.assertEqual(branches['WEST']['products'], 2)
		self.assertEqual(branches['WEST']['units'], '27')
		self.assertIsNotNone(branches['WEST']['last_seen'])

	def test_restore_script_has_the_branch_stock(self):
		response = self.client.get('/api/stock/restore/script/?branch=west')

		self.assertEqual(response.status_code, 200)
		self.assertIn('attachment; filename="restore_WEST_', response['Content-Disposition'])
		script = response.content.decode()
		self.assertIn("DECLARE @Branch varchar(50) = N'WEST';", script)
		self.assertIn('DECLARE @Apply bit = 1;', script)
		self.assertIn('DECLARE @CompanyId int = 412;', script)
		self.assertIn("(701, N'KELLOGG''S', N'702', N'600', 2.5, 15, 24)", script)
		# website rename and price win; empty code falls back to ProductID + 1 like the POS sync
		self.assertIn("(703, N'Website Name', N'704', N'', 1.2, 0, 3)", script)
		self.assertNotIn('Deleted Product', script)
		self.assertNotIn('Still Being Created', script)
		self.assertIn('Products:     2   (total units 27)', script)
		self.assertIn('IF @Apply = 1 COMMIT TRANSACTION; ELSE ROLLBACK TRANSACTION;', script)
		# uses the Quantum database's own company, not always 412
		self.assertIn('SELECT TOP (1) @CompanyId = companies.companyId FROM (', script)
		# POSViewer cannot add the same products halfway through (duplicate key)
		self.assertIn('FROM dbo.Products WITH (TABLOCKX, HOLDLOCK);', script)
		self.assertIn('WHERE NOT EXISTS (SELECT 1 FROM dbo.Products AS p WHERE p.ProductID = n.ProductID);', script)
		# rows older POSViewer versions saved under company 412 are moved to the right company
		self.assertIn('IF @CompanyId <> @OldPosCompanyId', script)

	def test_large_branch_is_split_into_batches(self):
		ProductCatalog.objects.bulk_create([
			ProductCatalog(branch='BIG', product_id=20000 + index, product_name=f'Item {index}', available_quantity=1)
			for index in range(1001)
		])

		script = self.client.get('/api/stock/restore/script/?branch=BIG').content.decode()

		self.assertEqual(script.count('INSERT INTO #RestoreProducts'), 3)

	def test_restore_requires_a_known_branch_and_login(self):
		self.assertEqual(self.client.get('/api/stock/restore/script/').status_code, 400)
		self.assertEqual(self.client.get('/api/stock/restore/script/?branch=MAIN').status_code, 400)
		self.assertEqual(self.client.get('/api/stock/restore/script/?branch=NOWHERE').status_code, 404)
		self.client.logout()
		self.assertEqual(self.client.get('/api/stock/restore/script/?branch=WEST').status_code, 302)
		self.assertEqual(self.client.get('/stock/restore/').status_code, 302)

	def test_main_computer_fallback_matches_the_website_script(self):
		from loraApi.branch_restore import MAIN_FALLBACK_SQL, build_main_fallback_sql

		response = self.client.get('/api/stock/restore/main-fallback.sql')

		self.assertEqual(response.status_code, 200)
		on_disk = MAIN_FALLBACK_SQL.read_text(encoding='utf-8')  # read_text normalises line endings
		self.assertEqual(on_disk, build_main_fallback_sql(), 'regenerate loraApi/sql/restore_branch_from_main.sql')
		self.assertIn('dbo.BranchProductCatalog', on_disk)


class QueueCancelTests(TestCase):
	def setUp(self):
		admin = get_user_model().objects.create_superuser(username='queue-cancel-admin', password='QueueCancelPass4182!')
		self.client.force_login(admin)

	def cancel(self, item_type, item_id):
		return self.client.post('/api/queue/cancel/', data=json.dumps({'type': item_type, 'id': item_id}), content_type='application/json')

	def test_waiting_items_can_be_cancelled_and_are_not_sent(self):
		DeletionRecord.objects.create(deletion_id='RET1', branch='BranchA', invoice='1001', status='pending')
		SalesReportRequest.objects.create(request_id='REP1', branch='BranchA', report_date='2026-11-24', status='pending')
		InvoiceReprintRequest.objects.create(request_id='PRN1', branch='BranchA', invoice='1002', status='pending')
		queue = {item['id']: item for item in self.client.get('/api/main-sync/').json()['queue']}
		self.assertTrue(all(queue[key]['cancellable'] for key in ['RET1', 'REP1', 'PRN1']))

		for item_type, item_id in [('cancellation', 'RET1'), ('sales_report', 'REP1'), ('invoice_reprint', 'PRN1')]:
			self.assertEqual(self.cancel(item_type, item_id).status_code, 200)

		self.assertEqual(DeletionRecord.objects.get(pk='RET1').status, 'cancelled')
		self.assertEqual(SalesReportRequest.objects.get(pk='REP1').status, 'cancelled')
		self.assertEqual(InvoiceReprintRequest.objects.get(pk='PRN1').status, 'cancelled')
		branch = self.client.get('/api/branch-sync/?branch=BranchA').json()
		self.assertEqual(branch.get('pending_deletions'), [])
		self.assertEqual(branch.get('pending_reports'), [])
		queue = {item['id']: item for item in self.client.get('/api/main-sync/').json()['queue']}
		self.assertEqual(queue['RET1']['status'], 'cancelled')
		self.assertFalse(queue['PRN1']['cancellable'])

	def test_items_already_sent_cannot_be_cancelled(self):
		SalesReportRequest.objects.create(request_id='REP2', branch='BranchA', report_date='2026-11-24', status='processing')
		response = self.cancel('sales_report', 'REP2')
		self.assertEqual(response.status_code, 409)
		self.assertEqual(SalesReportRequest.objects.get(pk='REP2').status, 'processing')
		self.assertEqual(self.cancel('sales_report', 'missing').status_code, 404)
		self.assertEqual(self.cancel('other', 'REP2').status_code, 400)
