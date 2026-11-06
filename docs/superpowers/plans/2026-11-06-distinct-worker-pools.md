# Distinct Worker Pools implementation plan

Approved design: preserve legacy unversioned queues, workers and histories. New
explicit governed-v1 inputs use disjoint versioned queues. Root owns publication,
live Schedule changes and promotion. Local tests are not cluster qualification.

1. Routing: add serialized SessionInput.worker_pool default legacy, fixed role
   queue/deployment mapping and explicit submit CLI option. Validate unknown pools
   before submission. Historical JSON defaults preserve legacy routing.
2. Worker configuration: default legacy remains unversioned; governed-v1 requires
   exact role deployment/build and pinned behavior. Governed workers never create
   or update the existing cleanup Schedule automatically.
3. Promotion: import the untracked reviewed foundation into this fresh checkout.
   Limit mutation to exact governed queue/deployment and fresh Pod-owned candidate
   Workflow+Activity pollers. Preserve legacy inventory, fresh CAS and no-ramp
   preconditions; uncertain write outcomes are journaled without retry.
4. Schedule adapter: raw protobuf CAS updates only future action task_queue,
   retaining opaque inputs and all spec/policy/state metadata. Explicit expected
   queue/conflict token and exact five-minute/SKIP schedule are required.
5. Focused unit tests establish legacy defaults, governed routing, invalid configs,
   promotion races/pollers, Schedule payload preservation/conflicts. Reuse owned
   isolated Temporal1.29.1 service for local queue separation/CAS evidence.
6. Follow-up: exact Kubernetes owner-chain inventory, current-worker discovery,
   drain/replay/rollback operations and inactive disjoint manifests/docs. Keep
   legacy seven-day retention/query needs; never infer submission closure from
   empty visibility or operator assertion.
