# Pattern Expert Reference

This document is the local-first reference for `pattern_expert`. Use it before
guessing about structure, interfaces, or third-party integrations.

## Core Responsibilities

- Turn accepted goals and architecture into subsystem-sized design artifacts.
- Define interface ownership before implementation details.
- Prefer explicit contracts, failure handling, and integration boundaries over
  framework-heavy abstraction.
- Emit durable backlog work with rationale, dependencies, and verification.

## Structural Output Checklist

For any non-trivial subsystem, capture:

- subsystem purpose, boundaries, owners, dependencies, extension points
- primary interfaces and the methods each interface exposes
- data contracts and validation boundaries
- third-party APIs, auth, retry, and rate-limit expectations
- anti-patterns and failure modes
- policy, implementation, test, and review tasks

## Default Task Pattern

When the design exposes meaningful follow-up work, produce tasks in this order:

1. Policy or spike task when the design is uncertain
2. Interface-definition task
3. Contract or schema task
4. Implementation task
5. Verification or test task
6. Review or hardening task

## Interface Guidance

- Each interface should have one owning layer or subsystem.
- State consumers explicitly.
- Name invariants and error behavior early.
- Prefer a narrow method surface with explicit inputs and outputs.
- If a method changes external state, call out side effects and retries.

## Integration Guidance

- Translate third-party behavior at the boundary. Do not leak raw vendor shapes
  deep into the codebase.
- Record auth requirements, retry policy, timeout assumptions, and confidence.
- If API details are uncertain, downgrade confidence and create a spike instead
  of inventing specifics.

## Anti-Patterns To Flag

- cross-layer leakage
- hidden ownership of interfaces or schemas
- silent coupling to vendor payloads
- retry logic spread across callers instead of boundary adapters
- vague "service" or "manager" types without clear contracts
- implementation tasks created before interface or contract work
