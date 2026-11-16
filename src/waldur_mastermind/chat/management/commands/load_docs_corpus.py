from pathlib import Path

from django.core.management.base import BaseCommand

from waldur_mastermind.chat.docs import corpus, search


class Command(BaseCommand):
    help = (
        "Load the documentation corpus that the AI assistant's search_docs tool "
        "searches. Defaults to the corpus shipped in the image "
        f"({corpus.DEFAULT_CORPUS_PATH}, or $WALDUR_DOCS_CORPUS_PATH). Does "
        "nothing when there is no corpus file, and writes nothing when the "
        "corpus is unchanged. Invalid lines are skipped; a corpus that cannot "
        "be read, or has no valid line, leaves the loaded sections as they "
        "were. Never fails, so that a bad corpus does not stop a deployment."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path", nargs="?", type=Path, help="Path to docs-corpus.jsonl.gz."
        )

    def handle(self, *args, **options):
        path = options["path"] or corpus.corpus_path()
        try:
            result = corpus.load_corpus(path)
        except corpus.CorpusError as exc:
            self.stderr.write(
                f"WARNING: invalid docs corpus {path}: {exc}. "
                "The loaded documentation is left unchanged."
            )
            return
        if result is None:
            self.stdout.write(f"No docs corpus at {path}, skipping.")
            return
        search.invalidate_cache()
        if result.skipped:
            self.stderr.write(
                f"WARNING: {result.skipped} invalid line(s) in docs corpus "
                f"{path} skipped."
            )
        self.stdout.write(
            f"Docs corpus loaded from {path}: {result.created} created, "
            f"{result.updated} updated, {result.deleted} deleted, "
            f"{result.unchanged} unchanged."
        )
