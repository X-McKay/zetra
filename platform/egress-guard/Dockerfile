# Egress guard init container (spec 5.5). Stdlib only: no dependencies to install.
# The base is pinned by digest; the built image is pinned by digest in the sandbox pod
# template and in the admission policy.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/egress-guard

COPY egress_guard/ /opt/egress-guard/egress_guard/

# No shell is needed at run time; the pod runs with readOnlyRootFilesystem and drops all
# capabilities, so the guard must work as an arbitrary non-root user.
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "-m", "egress_guard.main"]
