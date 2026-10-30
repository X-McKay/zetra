# Egress guard init container (spec 5.5). Stdlib only: no dependencies to install.
# Pin the base by digest when publishing; the built image is pinned by digest in the sandbox
# pod template and in the admission policy.
FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/egress-guard

COPY egress_guard/ /opt/egress-guard/egress_guard/

# No shell is needed at run time; the pod runs with readOnlyRootFilesystem and drops all
# capabilities, so the guard must work as an arbitrary non-root user.
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "-m", "egress_guard.main"]
