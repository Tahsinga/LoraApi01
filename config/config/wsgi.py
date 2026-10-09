"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/wsgi/
"""

import os

import django
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django.setup()
call_command('migrate', interactive=False, verbosity=0)
call_command('ensure_admin', verbosity=0)
call_command('restore_seed_state', verbosity=0)
application = get_wsgi_application()
