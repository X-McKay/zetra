# Temporal Agent Examples

## Durable Research Request

A research agent gathers sources, pauses for analyst approval, then publishes a report. Use one workflow with activities for planning, model execution, source retrieval, report storage, and publication. Use a signal for approval. Store the report outside workflow history and pass its artifact identifier.

```python
@workflow.defn
class ResearchWorkflow:
    @workflow.run
    async def run(self, request: ResearchRequest) -> ResearchResult:
        draft = await workflow.execute_activity(
            draft_report,
            DraftReportInput(request=request),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=TRANSIENT_RETRY_POLICY,
        )
        await workflow.wait_condition(lambda: self.approval is not None)
        if not self.approval.approved:
            return ResearchResult.rejected(draft.artifact_id)
        return await workflow.execute_activity(
            publish_report,
            PublishInput(
                artifact_id=draft.artifact_id,
                idempotency_key=request.request_id,
            ),
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=IDEMPOTENT_WRITE_RETRY_POLICY,
        )
```

Treat this as a structural example; adapt imports and APIs to repository-pinned Temporal and PydanticAI versions.

## Unknown Write Outcome

If `publish_report` times out after the remote system may have committed:

1. Query the destination using the idempotency key.
2. Return the existing publication if found.
3. Attempt creation only if no result exists.
4. Record reconciliation in telemetry.

## Approval Edge Cases

- Ignore or reject an approval for a different request identifier.
- Reject approval from an actor without the required role.
- Resolve races among approval, cancellation, and expiry deterministically.
- Never place an access token in a signal payload or workflow history.

## When Not to Use Temporal

A stateless FAQ agent that makes one model call and one read-only lookup usually belongs in the `ephemeral` execution class. Adding a workflow would add operational overhead without a durable business requirement.
