# Starter activation and local Temporal

The approved next steps are publishing the reviewed SDK, activating its exact
starter pin/quality gate, and providing a real local Temporal development mode.
The existing isolated worktrees and feature branches remain the working scope.
Feature-branch publication is authorized; merges, tags and production rollout are
not necessary to this increment.

Publish the reviewed e9bdc66 SDK and canonical preparation, retain reachable Git
commits, update the starter pin and lock together, and validate a fresh installation.
Activate the prepared cases/contract in normal validation and before wheel builds.
Keep assistant guidance consistent with the pinned capabilities and bundle the
canonical tree back into the SDK after committing it.

Local Temporal uses the unchanged SessionWorkflow and Activities, Runtime,
Definition, Dispatcher and Tape. A separate LocalTemporal development adapter
validates starts without manufacturing a production image/Release. Temporal owns
accepted progress at every model/tool boundary. Cold child processes reconstruct
from accepted tape; execute first reconstructs and compares its captured operation,
then executes once. An immutable attempt marker prevents redispatch after loss.
Candidate artifacts never select progress. Production workers remain unchanged.

Use private caller-owned local state, exact namespace/run handles, source/config
identity, a shared immutable FileStore, and per-execution flock admission. Keep
lock inodes and closed tombstones for the retained execution lifetime. Children
hold the lease through interpreter exit, check admission before project imports,
and use Linux parent-death handling. Cleanup acquires the lease, persists a terminal
fence and removes disposable workspaces before reporting deletion. Failed fencing
or removal remains unconfirmed. Replacement fences retired generations; cleanup
also fences late startup without an acknowledged binding.

Initial support is Linux trusted-host, cooperative in-process tools. A Python
audit guard rejects normal subprocess/fork/exec APIs in runner children so an
accidental tool subprocess cannot outlive the directly supervised runner. This
is compatibility enforcement, not isolation against hostile Python; direct native
syscalls and unmanaged/background execution are outside the supported profile.
No persistent warm process is claimed. Cold imports/replay add local overhead.

Expose LocalTemporal.worker/submit/result for explicitly configured clients and
persistent state. CLI dev can connect to an explicit loopback Temporal address
or start an SDK-managed local Temporal CLI dev server. The default dev command
keeps no Temporal. Use a pinned official CLI version or explicit binary, private
SQLite state when retained, and exact handles in reports. Model credentials stay
in the child environment, not Workflow inputs. Reports use accepted tape and
checkpoint contents, omit raw sessions/reasoning and remain unsigned.

Native tests must verify boundary scheduling, replay, accepted-prefix recovery,
loss after candidate without redispatch, cancellation/cleanup, late admission,
changed inputs, and persistent local-server restart. Existing fixture/live gates
are not relabeled as model or isolation qualification. The local Temporal dev
mode is an orchestration/recovery test surface; existing eval/quality fixture and
local-model modes remain available and unchanged in this increment.
