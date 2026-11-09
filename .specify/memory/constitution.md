<!--
Sync Impact Report
- Version change: (template, unversioned) → 1.0.0
- Principles defined: I–IX (all new; replaces the 5 template placeholders)
- Added sections: Technology & Security Constraints; Development Workflow & Quality Gates
- Removed sections: none
- Deferred TODOs: none
- Source: ~/context/ideas/roomba-kit/ideation.md (status: ready)
-->

# Roomba Kit Constitution

## Core Principles

### I. Local-First, Cloud-Optional

Every robot control and local-state capability MUST work with the iRobot cloud unreachable
or never configured. Cloud-backed features (room names and IDs, floor plans, mission history,
coverage paths, the live mission timeline, cloud password retrieval) are additive: when the
cloud is unavailable they MUST report an explicit `unavailable` state and MUST NOT degrade,
block or delay any local capability.

Rationale: the cloud API is unofficial and reverse-engineered, and its owner changed hands
after a bankruptcy. It is useful, never load-bearing.

### II. Single Connection Owner

Exactly one long-running service process MUST own each robot's local connection. The CLI,
HTTP API, MCP server and every external consumer MUST reach robots through that service and
MUST NOT open their own robot connections.

Rationale: robots accept one local client at a time; competing clients evict each other.

### III. One Capability Model, Three Surfaces

Every capability MUST be defined once in the core and exposed identically through the CLI,
the HTTP API and the MCP server: same names, same parameters, same semantics, same error
codes. Surface-only features are prohibited; a surface MAY add presentation (formatting,
help text), never behaviour.

Rationale: humans, programs and LLM agents must be able to move between surfaces without
relearning the kit.

### IV. Capability-Driven Model Support

Robot behaviour MUST be driven by each model's declared capabilities. Invoking an action a
model does not support MUST fail with an explicit `not_supported` error naming the model and
capability. Adding a model MUST NOT require changes to any surface. Each capability
declaration MUST carry a verification status.

Rationale: supported models differ materially (live position, bin vs tank, Clean Base,
mopping settings), and the kit must grow beyond the robots available for testing.

### V. Isolated Transport

Third-party transport libraries (local MQTT and cloud clients) MUST be wrapped behind the
kit's own interfaces. Their types, exceptions and payload shapes MUST NOT appear in the
public Python API, HTTP API, CLI output, MCP tools or event payloads.

Rationale: those libraries are unofficial and fast-moving; replacing one must be a local
change, not a rewrite.

### VI. Secrets Stay Out

Credentials (robot passwords, iRobot account credentials, API tokens) MUST live only in the
kit's documented configuration/secrets location outside the repository. They MUST NOT be
written to the repository, logs, HTTP/MCP responses, event payloads or error messages.
Credential files MUST be created with owner-only permissions.

Rationale: these credentials control physical devices in a home and a vendor account.

### VII. Stable, Versioned Contracts

The HTTP API, MCP tool set and event payloads MUST be versioned, and breaking changes MUST
bump the contract version. Robots MUST be identified by their stable robot ID (BLID); IP
addresses and display names are attributes, never identifiers.

Rationale: automations, bots and apps will hard-code these contracts; IPs and names change.

### VIII. Verified on Hardware, Honestly Labelled

Automated tests MUST run against a mocked transport so CI needs no robot. A capability MAY
only be documented as `verified` after it has been exercised against a real robot of that
model; otherwise it MUST be labelled `unverified` in its declaration, documentation and
discovery output.

Rationale: much of the protocol is known only from third-party reverse engineering;
users must be able to tell proven behaviour from expected behaviour.

### IX. Documentation Is Part of Done

A feature is not done until it is documented for all three surfaces with working examples.
The getting-started guide MUST take a new user from a robot on their Wi-Fi to a successful
command without reading source code.

Rationale: the kit exists to make building on top of it easy for humans and LLMs alike.

## Technology & Security Constraints

- Python 3.13+, packaged and run with `uv`; async throughout the core and service.
- Frameworks: Click (CLI), FastAPI (HTTP API), FastMCP (MCP server).
- The HTTP API MUST require authentication on every non-health endpoint.
- The service MUST start and stop with one command each and MUST be deployable to an
  always-on host other than the developer's machine.
- New runtime dependencies require a stated reason in the plan.

## Development Workflow & Quality Gates

- Every plan MUST include a Constitution Check against Principles I–IX; violations require a
  justification in the plan's complexity tracking or a constitution amendment.
- Every change MUST pass linting, type checking and the mocked-transport test suite in CI.
- Contract changes (API, MCP, events) MUST update contract tests and documentation in the
  same change.
- Capability verification status changes MUST cite the hardware run that justified them.

## Governance

This constitution supersedes other project practices. Amendments are made by pull request
editing this file, with a version bump under semantic versioning: MAJOR for removing or
redefining a principle, MINOR for adding a principle or materially expanding guidance, PATCH
for clarifications. Reviews of specs, plans and pull requests MUST check compliance.

**Version**: 1.0.0 | **Ratified**: 2026-11-08 | **Last Amended**: 2026-11-08
