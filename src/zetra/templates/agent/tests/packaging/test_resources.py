from importlib.resources import files


def test_agent_resources_are_available():
    resources = files("agent_package.agents.document_reader")
    assert "document_reader" in resources.joinpath("agent.yaml").read_text()
    assert "read_document" in resources.joinpath("instructions/base.md").read_text()
