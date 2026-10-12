from datetime import date

from scheduled_report.contracts import ReportRequest, ReportResult, WikiPage

from zetra.sdk.testing import call, final


def test_publishes_one_page_for_the_scheduled_day(agent):
    agent.gateway("metrics").stub("daily_metrics", {"requests": 120000, "p99_ms": 410})
    agent.model.script(
        call("publish_page", body="120000 requests, p99 410 ms."),
        final(ReportResult(page_title="Service report 2026-10-11")),
    )
    run = agent.run(ReportRequest(day=date(2026, 10, 11)))
    assert run.output.page_title == "Service report 2026-10-11"
    assert agent.receiver("wiki").commits == [
        WikiPage(title="Service report 2026-10-11", body="120000 requests, p99 410 ms.")
    ]
