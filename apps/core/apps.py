from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'

    def ready(self):
        # New records saved outside a gym website request take their owner's side (apps.core.sides).
        from django.db.models.signals import pre_save
        from .sides import inherit_side
        pre_save.connect(inherit_side, dispatch_uid='gym_side_inherit')
