"""Deterministic extractive fixture, not a language model or quality benchmark."""

import re

from .models import Answer, Document


class OfflineAnswerFixture:
    def answer(self, question: str, documents: tuple[Document, ...], instructions: str) -> Answer:
        # This fixture does not simulate prompt understanding. It extracts a
        # supported sentence by content-word overlap and otherwise abstains.
        ignored = {
            "what",
            "is",
            "the",
            "how",
            "can",
            "for",
            "of",
            "a",
            "an",
            "to",
            "do",
            "i",
            "through",
            "before",
        }
        terms = set(re.findall(r"[a-z]+", question.lower())) - ignored
        matches = []
        for document in documents:
            for sentence in document.content.splitlines():
                words = set(re.findall(r"[a-z]+", sentence.lower()))
                overlap = len(terms & words)
                if overlap and sentence.strip():
                    matches.append((overlap, document.identifier, sentence.strip()))
        if not matches:
            return Answer(
                "The approved documents do not support an answer. Request human review.",
                (),
                "needs_review",
            )
        _, identifier, sentence = sorted(matches, key=lambda item: (-item[0], item[1], item[2]))[0]
        return Answer(sentence, (identifier,), "answered")
