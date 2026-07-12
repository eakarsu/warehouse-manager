## Summary

Describe the user-visible outcome and source evidence.

## Validation

- [ ] `python scripts/validate_app.py` passes.
- [ ] `python scripts/smoke_test.py` passes.
- [ ] I manually checked affected behavior when appropriate.

## Safety and architecture

- [ ] No secrets, local source links, SQLite sidecars, or sensitive records are included.
- [ ] Non-AI features work without an OpenRouter key and do not call an AI provider.
- [ ] Any AI behavior is clearly labeled and starts only from an explicit user action.
- [ ] Authorization, deployment, and data-handling implications are documented.
