from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.approvals'

    def ready(self):
        from . import handlers  # noqa: F401  (registers every kind of content that needs approval)
