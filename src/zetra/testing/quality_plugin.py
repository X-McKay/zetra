"""Record fresh pytest collection and every execution phase for a local gate."""

import json
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--zetra-report", required=True, help="private quality evidence output")


def pytest_configure(config):
    config._zetra_quality = {"collected": [], "all_collected": [], "deselected": [], "reports": []}


def pytest_itemcollected(item):
    item.config._zetra_quality["all_collected"].append(item.nodeid)


def pytest_deselected(items):
    for item in items:
        item.config._zetra_quality["deselected"].append(item.nodeid)


def pytest_collection_finish(session):
    session.config._zetra_quality["collected"] = [item.nodeid for item in session.items]


def pytest_runtest_logreport(report):
    # The active plugin instance is scoped to this fresh interpreter.
    _reports.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "wasxfail": hasattr(report, "wasxfail"),
        }
    )


_reports = []


def pytest_sessionfinish(session, exitstatus):
    report = {**session.config._zetra_quality, "reports": _reports, "exitcode": int(exitstatus)}
    target = Path(session.config.getoption("--zetra-report"))
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report), encoding="utf-8")
    temporary.replace(target)
