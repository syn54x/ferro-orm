# IR-first migration guide (living document)

This guide is updated continuously during the IR-first roadmap execution.

## Purpose

- Capture user-facing migration impact as work lands.
- Provide upgrade guidance before each phase closes.
- Ensure breaking changes are documented with mitigation paths.

## How to use this guide

- Add entries as part of issue completion for roadmap work.
- Group entries by phase.
- For each entry, include:
  - linked issue
  - change summary
  - impact level (`none`, `minor`, `breaking`)
  - required user action
  - compatibility window/deprecation timeline (if applicable)

## Phase entries

### Phase 0

No user-facing runtime behavior changes expected.

| Issue | Change | Impact | User action | Notes |
| --- | --- | --- | --- | --- |
| [#72](https://github.com/syn54x/ferro-orm/issues/72) | IR contract RFC definition | none | none | design-only; artifact: `docs/rfc/ir-contracts-v1.md` |
| [#73](https://github.com/syn54x/ferro-orm/issues/73) | Invariant specification | none | none | design-only; artifact: `docs/solutions/patterns/ir-invariants.md` |
| [#74](https://github.com/syn54x/ferro-orm/issues/74) | Golden vectors + CI harness skeleton | none | none | infra-only; artifacts: `tests/fixtures/ir_vectors/`, `tests/test_ir_vectors_contract.py` |

### Phase 1

_TBD_

### Phase 2

_TBD_

### Phase 3

_TBD_

### Phase 4

_TBD_

### Phase 5

_TBD_

### Phase 6

_TBD_

### Phase 7

_TBD_
