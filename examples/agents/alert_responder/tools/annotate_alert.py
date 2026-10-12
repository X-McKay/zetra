from zetra.sdk import tool

from ..contracts import Alert, Annotation


@tool("alerts.write")  # reversible write listed in zetra.toml [reversible]: no human approval
def annotate_alert(note: str, *, alert: Alert) -> Annotation:
    """Attach a note to the alert explaining the decision."""
    return Annotation(alert_id=alert.alert_id, note=note)
