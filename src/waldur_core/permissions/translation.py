from modeltranslation.translator import TranslationOptions, translator

from .models import Role


class RoleTranslationOptions(TranslationOptions):
    fields = ('description',)


translator.register(Role, RoleTranslationOptions)
