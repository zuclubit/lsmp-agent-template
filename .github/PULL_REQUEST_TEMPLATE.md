## Summary

<!-- What changed and why? Be specific — describe the problem this PR solves and how. -->

## Type of Change

- [ ] New agent
- [ ] Bug fix (validation, execution, or orchestration logic)
- [ ] Security fix
- [ ] Config / threshold change
- [ ] Documentation
- [ ] Test addition
- [ ] Refactor (no behavior change)

## Testing Done

- [ ] `pytest tests/ -m unit` passes locally (no network required)
- [ ] `pytest tests/agent-tests/` passes with mocked Anthropic client
- [ ] New unit tests added for all new tools or logic
- [ ] Integration tests updated if external API contracts changed
- [ ] Failure scenario tests added for any new destructive actions

## Security Checklist

- [ ] No secrets, tokens, credentials, or API keys committed
- [ ] No real PII, PHI, or CUI in test fixtures (synthetic data only)
- [ ] Path traversal not introduced (`read_file` uses relative paths only, no `../`)
- [ ] SOQL changes use SELECT only — no DELETE, UPDATE, INSERT, MERGE, DROP, GRANT
- [ ] No new outbound HTTP calls to arbitrary URLs (egress is restricted)

## Compliance Checklist

- [ ] No GDPR-relevant changes  /  GDPR impact assessed and documented: ___
- [ ] No SOX-relevant changes   /  Change control ticket raised: ___
- [ ] No FedRAMP-relevant changes  /  Security review requested: ___
- [ ] Audit trail unaffected  /  OR: audit log changes reviewed by `@zuclubit/security`

## Halcon Metrics Impact

- [ ] No change to agent behavior — Halcon metrics unaffected
- [ ] Changed agent behavior — describe expected impact on metrics below
- [ ] Changed thresholds in `config/agents.yaml` — baseline metrics updated
- [ ] Expected Halcon metric impact: <!-- e.g., "convergence_efficiency should improve by ~0.05 due to fewer wasted retries" -->

## Related Issues

<!-- Closes #, Fixes #, or Related to # -->
