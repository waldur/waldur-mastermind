from modeltranslation.translator import TranslationOptions, translator

from .models import Category, CategoryGroup


class CategoryTranslationOptions(TranslationOptions):
    fields = ('title', 'description')


translator.register(Category, CategoryTranslationOptions)


class CategoryGroupTranslationOptions(TranslationOptions):
    fields = ('title', 'description')


translator.register(CategoryGroup, CategoryGroupTranslationOptions)
