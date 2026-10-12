from typing import Literal

from pydantic import Field

from zetra.sdk import Contract


class Question(Contract):
    text: str = Field(max_length=500)


class Answer(Contract):
    kind: Literal["answer"] = "answer"
    text: str = Field(max_length=2000)
    sources: list[str]


class NotFound(Contract):
    kind: Literal["not_found"] = "not_found"
    text: str


Input, Output = Question, Answer | NotFound
