from django.test import TestCase
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from loraApi.state_store import load_state
import json


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
