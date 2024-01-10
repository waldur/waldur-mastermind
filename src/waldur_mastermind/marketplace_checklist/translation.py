from modeltranslation.translator import TranslationOptions, translator

from .models import Checklist, Question


class QuestionTranslationOptions(TranslationOptions):
    fields = ("solution", "description")


class ChecklistTranslationOptions(TranslationOptions):
    fields = ("name", "description")


translator.register(Question, QuestionTranslationOptions)
translator.register(Checklist, ChecklistTranslationOptions)
