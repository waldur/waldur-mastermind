from modeltranslation.translator import TranslationOptions, translator

from .models import Category


class CategoryTranslationOptions(TranslationOptions):
    fields = ('title', 'description')


translator.register(Category, CategoryTranslationOptions)
