---
name: Bug Report
about: Report a defect in an agent, tool, or pipeline component
title: "[BUG] <agent-name>: <brief description>"
labels: bug
assignees: ''
---

## Agent / Component

<!-- Which agent or component is affected? -->
- [ ] orchestrator-agent
- [ ] planning-agent
- [ ] validation-agent
- [ ] security-agent
- [ ] execution-agent
- [ ] debugging-agent
- [ ] migration pipeline (ETL)
- [ ] Halcon observability
- [ ] Other: ___

## Severity

<!-- Select one -->
- [ ] P0 — Production down, data loss in progress
- [ ] P1 — Critical agent failure, migration blocked
- [ ] P2 — Major agent misbehavior, workaround available
- [ ] P3 — Minor issue, no production impact
- [ ] P4 — Cosmetic or documentation issue

## Environment

- **Environment:** development / staging / production
- **Python version:**
- **Commit SHA:**
- **Migration run ID (if applicable):**

## Reproduction Steps

<!-- Provide exact steps to reproduce the behavior -->

1.
2.
3.

## Expected Behavior

<!-- What should have happened? -->

## Actual Behavior

<!-- What actually happened? Include any error messages verbatim. -->

## Logs Excerpt

<!-- Paste relevant log lines here. Remove any PII, secrets, or CUI before posting. -->

```
<paste logs here>
```

## Halcon Session Metrics

<!-- If available, paste the relevant session record from .halcon/retrospectives/sessions.jsonl -->
<!-- Remove any sensitive field values before posting. -->

```json
<paste session metrics here>
```

## Additional Context

<!-- Any other context that might help diagnose the issue. -->
<!-- Do NOT include real customer data, credentials, or production secrets. -->
