---

## date: 2026-04-29

topic: named-connections-role-routing

# Named Connections and Role-Safe Routing

## Problem Frame

Ferro currently presents a single active database engine to Python callers. That keeps the API simple, but it blocks users who need to use the same database through different Postgres roles, such as a Supabase application role for user-facing data access and a service or pipeline role for trusted internal work.

The feature should let users register multiple named connections and choose the intended connection at the operation or transaction boundary. The default experience should stay ergonomic for single-database apps, while multi-role apps get explicit routing, role-safe transaction behavior, separate pool settings, and clear guardrails against accidental privilege mixing.

---

## Actors

- A1. Application developer: Configures Ferro connections and writes model/query code.
- A2. User-facing application runtime: Handles normal product requests through the least-privileged app connection.
- A3. Internal service or pipeline runtime: Runs trusted background work through a separate elevated connection.
- A4. Migration or setup process: Creates or updates schema using an explicitly chosen connection.
- A5. Downstream implementation agent: Plans and builds the feature without inventing public API semantics.

---

## Key Flows

- F1. Single-connection app startup
  - **Trigger:** A developer uses Ferro as they do today with one database URL.
  - **Actors:** A1, A2
  - **Steps:** The app calls `ferro.connect(url)`. Ferro registers that connection as `"default"` and makes it the default connection. Existing unqualified model, query, transaction, and raw SQL calls continue to work.
  - **Outcome:** Existing apps do not need to learn named routing unless they add more connections.
  - **Covered by:** R1, R2, R5, R14
- F2. Multi-role Supabase startup
  - **Trigger:** A developer needs separate app and service-role database access.
  - **Actors:** A1, A2, A3
  - **Steps:** The app registers an app connection with `default=True` and a service connection with its own name and pool settings. Normal model calls use the app connection. Pipeline code opts into the service connection explicitly.
  - **Outcome:** Both roles can coexist in one process without reconnecting global state or sharing a pool.
  - **Covered by:** R1, R2, R3, R4, R6, R12
- F3. Service transaction with inherited routing
  - **Trigger:** A pipeline needs a unit of work to run through the service connection.
  - **Actors:** A3
  - **Steps:** Pipeline code enters `async with ferro.transaction(using="service")`. Unqualified model and raw SQL operations inside the block inherit the transaction connection. Any attempt to route part of the transaction to another connection fails clearly.
  - **Outcome:** The transaction is ergonomic and cannot silently become a cross-connection pseudo-transaction.
  - **Covered by:** R7, R8, R9, R10, R11
- F4. Schema setup on an explicit connection
  - **Trigger:** A developer wants Ferro to create tables or run setup against a specific role.
  - **Actors:** A1, A4
  - **Steps:** The developer chooses the connection when enabling `auto_migrate` or calling schema creation APIs. Ferro does not assume that the default app connection should have migration privileges.
  - **Outcome:** Schema writes are deliberate and can be restricted to a migration-capable connection.
  - **Covered by:** R13, R15

---

## Requirements

**Connection registration and defaults**

- R1. Ferro must support registering more than one active connection in a process, each identified by a stable string name.
- R2. Calling `ferro.connect(url)` without a name must remain valid and must register the connection as `"default"`.
- R3. `ferro.connect(url, name="...", default=True)` must make that named connection the default for unqualified operations.
- R4. Ferro must provide a way to change the default connection after registration, such as `ferro.set_default_connection("app")`.
- R5. If more than one connection exists and no default has been selected, unqualified operations must fail with a clear error instead of guessing.

**Pool configuration**

- R6. Pool configuration must belong to the named connection, because app, service, pipeline, replica, and test connections can have different concurrency and lifetime needs.
- R7. Ferro should expose pool configuration as a Ferro API object or explicit keyword arguments rather than overloading database URL query parameters.
- R8. Native database URL settings, such as Postgres TLS parameters, must remain in the connection URL when they are part of the database driver's normal URL contract.
- R9. The initial pool configuration surface should cover the common operational needs: maximum connections, minimum connections if supported, acquire timeout, idle timeout, max lifetime, and connection health checking if supported by the backend.
- R10. Pool configuration must be optional; a connection with no explicit pool settings should use conservative Ferro defaults.

**Routing and operation ergonomics**

- R11. Ferro must support explicit per-operation routing through a named connection, using a concise API such as `Model.using("service")`, query-level `using`, or an equivalent fluent surface.
- R12. The connection resolution order must be: explicit operation routing first, active transaction connection second, default connection third, and clear error last.
- R13. Raw SQL APIs must participate in the same routing model as ORM APIs, including transaction inheritance.

**Transactions**

- R14. `ferro.transaction(using="name")` must bind the transaction to exactly one named connection for its lifetime.
- R15. Unqualified ORM and raw SQL calls inside a transaction must inherit the transaction's named connection.
- R16. Explicitly routing an operation to a different connection inside an active transaction must fail clearly unless Ferro later introduces an explicit cross-connection transaction feature.
- R17. Nested transactions must inherit the parent transaction connection unless the nested call specifies the same connection; specifying a different connection must fail clearly.

**Identity map and object safety**

- R18. Ferro's identity map must isolate instances by connection name as well as model and primary key, so an object loaded through an elevated role cannot be reused to satisfy an app-role query.
- R19. Model instances loaded from a named connection should carry enough internal state for later saves or relationship operations to prefer the same connection when no stronger routing context exists.

**Schema management and migrations**

- R20. `auto_migrate` and explicit schema creation APIs must run against a specific named connection.
- R21. Documentation must recommend using a migration-capable connection for schema changes rather than assuming the default app connection has DDL privileges.
- R22. Ferro must not silently run migrations across all registered connections.

**Security and Supabase guidance**

- R23. Documentation must warn that elevated service credentials must stay server-side and should not be exposed in public clients.
- R24. Documentation must recommend least-privileged custom Postgres roles where possible, with service-style privileges reserved for trusted internal processes.
- R25. Ferro must redact connection credentials in logs and user-facing errors.

---

## Acceptance Examples

- AE1. **Covers R1, R2, R14.** Given an existing app calls `await ferro.connect(url)`, when it calls `await User.create(...)`, the operation uses the implicitly registered `"default"` connection and does not require `using`.
- AE2. **Covers R3, R5, R11.** Given `app` and `service` connections are registered and `app` is marked default, when code calls `await User.all()`, it uses `app`; when code calls `await PipelineEvent.using("service").create(...)`, it uses `service`.
- AE3. **Covers R12, R14, R15.** Given code is inside `async with ferro.transaction(using="service")`, when it calls `await PipelineEvent.create(...)`, the model call uses the service transaction connection without repeating `using="service"`.
- AE4. **Covers R16, R17.** Given code is inside `async with ferro.transaction(using="service")`, when it attempts `await User.using("app").create(...)`, Ferro raises an error explaining that a transaction cannot switch from `service` to `app`.
- AE5. **Covers R18.** Given row `User(id=1)` is loaded through `service`, when the same row is later queried through `app`, Ferro must not return the service-loaded Python instance from the identity map.
- AE6. **Covers R6, R7, R10.** Given the app connection has `max_connections=20` and the service connection has `max_connections=5`, each named connection uses its own pool settings and neither setting affects the other.
- AE7. **Covers R20, R22.** Given `app` and `service` connections are registered, when `create_tables(using="service")` runs, Ferro creates schema only through `service` and does not run DDL on `app`.
- AE8. **Covers R23, R25.** Given a Supabase connection fails, the raised error and logs do not reveal passwords, service credentials, or full secret-bearing URLs.

---

## Success Criteria

- Existing single-connection Ferro apps continue to work without API changes.
- A user can run app-role and service-role Supabase/Postgres work in the same process without resetting global engine state.
- The happy path for a service transaction is concise enough that users do not repeat `using="service"` on every call inside the block.
- The API makes privilege boundaries visible at connection setup and transaction entry, not hidden in model definitions or global magic.
- Transaction behavior never implies atomicity across more than one named connection.
- A downstream planner can implement the feature without deciding the public API precedence rules, default behavior, pool ownership model, or Supabase safety posture from scratch.

---

## Scope Boundaries

- The first version does not need automatic read/write splitting or policy routers like Django's `DATABASE_ROUTERS`.
- The first version does not need distributed or two-phase transactions across named connections.
- The first version does not need cross-database relationships or joins.
- The first version does not need dynamic tenant connection creation beyond the same named registration primitives.
- The first version does not need per-model static binding, though the API should not preclude it later.
- Supabase is the motivating Postgres deployment target, but the feature should stay framed as named connection support rather than a Supabase-only capability.
- The feature should not add support for new database backends beyond Ferro's current supported backend contract.

---

## Key Decisions

- Named connections are the core abstraction: They cover same-database different-role access, multiple databases, replicas, and future routing without inventing a Supabase-only concept.
- Pool settings live on the connection: This matches operational reality and avoids global pool settings that are wrong for either app traffic or pipeline work.
- A default connection is allowed: It preserves Ferro's ergonomic model and avoids forcing `using` everywhere in normal application code.
- Transactions create an ambient routing context: This makes service-role units of work concise while keeping all operations on one connection.
- Explicit cross-connection operations inside a transaction are errors: This prevents accidental pseudo-atomic workflows.
- Identity-map keys include connection identity: This is necessary for role safety when different roles can see different rows or columns.
- Migrations are connection-specific: Schema writes should be deliberate and not fan out across registered connections.
- Credentials must be redacted: Multi-role support increases the chance that elevated credentials pass through Ferro configuration.

---

## Proposed UX Shape

This section is illustrative product UX, not an implementation prescription.

```python
await ferro.connect(
    app_url,
    name="app",
    default=True,
    pool=ferro.PoolConfig(max_connections=20, min_connections=2),
)

await ferro.connect(
    service_url,
    name="service",
    pool=ferro.PoolConfig(max_connections=5, acquire_timeout=30),
)

await User.create(email="user@example.com")  # Uses app.

async with ferro.transaction(using="service"):
    await PipelineEvent.create(kind="sync_started")  # Uses service.
    await ferro.execute("select set_config('app.pipeline', $1, true)", "sync")
```

Connection resolution:

```text
explicit operation using
  -> active transaction connection
  -> default named connection
  -> clear "no connection selected" error
```

---

## Dependencies / Assumptions

- Ferro's typed backend work is the right foundation for this feature; named connections should build on explicit engine handles rather than revive URL-string or generic-pool dispatch.
- Direct Supabase/Postgres connections can represent the needed role boundary through distinct database URLs or credentials.
- Users who need multiple roles in one process value explicitness over fully automatic routing.
- Keeping routers out of v1 reduces API and testing complexity without blocking the app/service-role use case.

---

## Outstanding Questions

### Resolve Before Planning

- None.

### Deferred to Planning

- [Affects R7, R9][Technical] Which pool settings are supported uniformly across SQLite and Postgres, and which need backend-specific validation?
- [Affects R11, R12][Technical] Should the primary explicit routing API be `Model.using("name")`, query terminal `using="name"` arguments, or both for parity with the current query builder shape?
- [Affects R19][Technical] How should instance stickiness interact with an active transaction when they disagree?
- [Affects R20][Technical] Should `auto_migrate=True` remain on `connect()` only, or should schema setup move toward an explicit `create_tables(using="name")` first-class UX?
- [Affects R26][Needs research] What exact redaction behavior should be shared across Rust logs, Python exceptions, and test diagnostics?

---

## Next Steps

-> /ce-plan for structured implementation planning.
