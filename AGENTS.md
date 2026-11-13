# Repository instructions

This repository contains a deployable framework; `runtime/` is source, not a live deployment. Read BOOTSTRAP.md for deployment, docs/architecture.md for changes to system behavior, and docs/permissions.md before touching identity or isolation.

- Never run runtime entrypoints directly in this checkout. Use scripts/aw.py on a new target or scripts/verify.py, which constructs an isolated deployment.
- Preserve durable state, stable operation IDs, session ownership and scope checks. Examples and fixtures are synthetic; never write them to a production ledger.
- Validate changes with `python3 scripts/verify.py --node` and `python3 scripts/release_check.py`; Python must be >=3.11. External acceptance remains separate.
- Public source must not contain real member identities, credentials, browser profiles, live config, state databases, internal business artifacts or original chat contents.
- Keep deployment behavior in scripts, conditional procedures in docs/SOP/Skill, and this file a short index. New runtime requirements need configuration and acceptance coverage.
