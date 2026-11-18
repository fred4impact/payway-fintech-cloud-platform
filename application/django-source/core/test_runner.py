"""Discover tests in application/django-source when no labels are given.

manage.py lives at the repository root, so Django's default discovery (".")
would search the repo root and find nothing.
"""
from django.conf import settings
from django.test.runner import DiscoverRunner


class SourceDiscoverRunner(DiscoverRunner):
    def build_suite(self, test_labels=None, *args, **kwargs):
        if not test_labels:
            test_labels = [str(settings.BASE_DIR)]
        return super().build_suite(test_labels, *args, **kwargs)
