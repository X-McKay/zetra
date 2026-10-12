from handbook_qa.contracts import Answer, NotFound, Question

from zetra.sdk.testing import call, final


def test_answers_with_citations(agent):
    agent.gateway("docs").stub(
        "search_documents",
        [{"id": "expense-policy", "text": "Submit expenses within 30 days."}],
        params=["query"],
    )
    agent.model.script(
        call("search_documents", query="expense deadline"),
        final(Answer(text="Submit expenses within 30 days.", sources=["expense-policy"])),
    )
    run = agent.run(Question(text="When must I submit expenses?"))
    assert run.output.sources == ["expense-policy"]
    assert agent.gateway("docs").calls == [("search_documents", {"query": "expense deadline"})]


def test_not_found_is_a_typed_outcome(agent):
    agent.gateway("docs").stub("search_documents", [], params=["query"])
    agent.model.script(
        call("search_documents", query="parking"),
        final(NotFound(text="The handbook does not cover parking.")),
    )
    assert agent.run(Question(text="Where do I park?")).output.kind == "not_found"
