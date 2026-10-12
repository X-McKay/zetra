from alert_responder.contracts import Alert, Annotation, NoAction, Page, Paged

from zetra.sdk.testing import call, final

ALERT = Alert(
    alert_id="ALR-20261012", service="checkout-api", severity="critical", message="p99 latency 4s"
)
PAGE = Page(service="checkout-api", urgency="high", summary="checkout-api degraded, p99 4s")


def test_critical_alert_pages_after_approval(agent):
    agent.gateway("status").stub("read_status", "status: degraded").stub(
        "recent_deploys", "none in 2h"
    )
    agent.model.script(
        call("page_oncall", urgency="high", summary=PAGE.summary),
        final(Paged(reason="critical and degraded, no recent deploy")),
    )
    run = agent.run(ALERT)
    assert run.approvals.pending == [PAGE] and agent.receiver("pager").commits == []
    run.approve_all(actor="oncall:bob")
    assert run.output == Paged(reason="critical and degraded, no recent deploy")
    assert agent.receiver("pager").commits == [PAGE]


def test_info_alert_is_annotated_not_paged(agent):
    agent.gateway("status").stub("read_status", "status: healthy")
    agent.model.script(
        call("annotate_alert", note="Info severity; service healthy; no page."),
        final(NoAction(reason="policy: info alerts are never paged")),
    )
    run = agent.run(ALERT.model_copy(update={"severity": "info"}))
    assert run.output.kind == "no_action"
    assert agent.receiver("alerts").commits == [
        Annotation(alert_id="ALR-20261012", note="Info severity; service healthy; no page.")
    ]
    assert agent.receiver("pager").commits == []


def test_rejected_page_is_not_committed(agent):
    agent.gateway("status").stub("read_status", "status: degraded").stub(
        "recent_deploys", "none in 2h"
    )
    agent.model.script(
        call("page_oncall", urgency="high", summary=PAGE.summary),
        final(NoAction(reason="page was rejected by the approver")),
    )
    run = agent.run(ALERT)
    run.reject_all(actor="oncall:bob", reason="known issue")
    assert run.output.kind == "no_action" and agent.receiver("pager").commits == []
