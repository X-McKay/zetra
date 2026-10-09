# Approved-document assistant starter

A small Python 3.11+ application showing typed contracts, explicit dependencies,
bounded document access and deterministic evaluation. It uses only the standard
library. Copy this directory to start a framework-neutral application.

The answerer is an **offline extractive fixture**, not a language model. It selects
an existing line using word overlap and abstains when there is no overlap. Its
passing cases demonstrate specific fixture behavior, input/output contracts and
local tool access checks. They do not establish semantic accuracy, prompt injection
resistance, model quality or production security.

From the repository root:

```sh
python3 examples/developer-starter/run.py \
  --question 'Where can I find the staff handbook?' --document staff-handbook

PYTHONPATH=examples/developer-starter/src python3 -m unittest discover \
  -s examples/developer-starter/tests -v

python3 examples/developer-starter/evaluate.py \
  --output build/developer-starter-evaluation.json
```

In the repository's locked developer environment, replace `python3` with
`uv run --locked python` in each command. The run and evaluation scripts locate
their own source/configuration, so they also work from another working directory
when invoked with an absolute script path. The test command supplies the source
path explicitly. No model keys, cluster, network access or additional packages are
required. Only the optional evaluation report writes application output.

Example response:

```json
{"outcome":"answered","source_ids":["staff-handbook"],"text":"The staff handbook is available in the approved document portal."}
```

Try `--document private-budget` to observe an access denial, even though a synthetic
fixture with that ID exists on disk. An unknown question such as `Rotate database
credentials?` with `--document staff-handbook` produces `needs_review`.

## Where to make changes

| Path | Responsibility |
| --- | --- |
| `application.toml` | Curator-owned approved IDs, confined fixture paths, request document limit |
| `src/document_assistant/models.py` | Strict request and answer wire contracts |
| `src/document_assistant/dependencies.py` | Typed document and answerer interfaces |
| `src/document_assistant/factory.py` | Validated construction and boundary checks |
| `src/document_assistant/tools.py` | Approved local file reader; rejects unknown IDs, symlinks and oversized files |
| `src/document_assistant/fixture.py` | Explicit deterministic answerer fixture |
| `instructions/system.md` | Instruction artifact supplied to the answerer interface |
| `tests/` | Unit, smoke, local tool integration and command end-to-end checks |
| `evals/cases.jsonl` | Versioned supported, abstention, denial and malformed-input scenarios |

`DocumentReader.read(identifier: str) -> Document` reads one approved document.
`Answerer.answer(question, documents, instructions) -> Answer` is the replaceable
answerer boundary. `create_application(root)` injects both implementations and
validated settings. Requests contain `question` and `document_ids`; answers contain
`text`, `source_ids` and `outcome` (`answered` or `needs_review`). An answer may cite
only documents supplied in that request. This citation check validates membership;
it does not prove that the cited content supports the answer.

The configuration is illustrative application configuration, not a platform policy
schema. Its allowlist is fixed for the example and does not implement authenticated
per-user authorization. Instructions are passed to the fixture but it does not
interpret them as a model would. Questions and document text cannot modify the file
reader's allowlist. These checks are cooperative Python checks in a trusted local
process, not a filesystem sandbox, concurrency-safe file isolation or OS enforcement.
Do not execute untrusted code using this helper.

To add a real model, implement the answerer interface with explicit timeouts, token
limits and approved transport, then evaluate that concrete model/configuration.
Add representative queries, unsupported questions, malicious document content,
citation grounding, retrieval failures, user authorization and cost/latency cases.
Keep deterministic contract tests; record model evaluations separately with model,
prompt, dataset and adapter versions. Independent backend authorization and runtime
isolation remain required before production use.
