# Architecture

The external session worker orchestrates a provision/advance/execute/cleanup
Workflow. ADK, its model client and registered tool bodies run inside one private
OpenShell sandbox. The worker reads release metadata and validates responses; it
never imports agent factories.

A bridge model and tool wrappers describe each operation. Temporal schedules an
execute Activity with one attempt. The sandbox returns a candidate receipt and
immutable artifacts. External validation checks the predecessor, descriptor,
contents and workspace before returning the Activity result. That Temporal-accepted
position alone selects subsequent progress. Uploaded candidates cannot advance it.

A warm ADK continuation waits at the boundary. A later authenticated advance with
the exact accepted candidate injects its recorded result. Cache loss reconstructs
from the accepted tape without executing recorded effects. Safe replacement first
confirms the old immutable sandbox instance is gone. Unknown execute outcomes
remain unknown, including when their candidate bytes are present.

Small artifacts are inline up to a bounded 128 KiB raw-byte budget. Larger objects
use verified transfer and chunked upload. Serialized Activity payloads are checked
against the converter's actual representation. Workspace snapshots reject links,
special files and unsafe paths before acceptance or restoration.

The worker uses one adapter, tape, task queue and lock per session. The release
binds its image, source, model/policy configuration and SDK version. Session keys
are retained privately outside Workflow history. Existing Workflow attachment
requires the original start identity, including key fingerprint, to match. The
private record pins both control-plane endpoints, the OpenShell CA and client
certificate fingerprints, and workspace. A changed gateway is rejected before
cleanup or replacement. Interactive forwards must retain their addresses.

OpenShell provisioning creates a fresh volume and runtime. Trusted bytecode in the
read-only image reduces compilation work; no writable cache or initialized agent
state is shared between sessions. Stop-before-delete preserves grace periods and
immutable deletion confirmation. Cleanup failures remain visible to callers.

The runner pins endpoint and certificate configuration; it does not detect a
control plane rebuilt behind identical endpoints and keys. Cluster state must
remain stable for the lifetime of retained sessions. Distributed leases and a
platform-issued immutable cluster identity are production qualification gates.
