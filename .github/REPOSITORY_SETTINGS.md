# Recommended repository settings

- Protect `main` and require the `Validate and smoke test` status check.
- Require pull requests and resolved conversations; block force pushes and branch deletion.
- Enable private vulnerability reporting, secret scanning, push protection, Dependabot alerts, and CodeQL default setup for Python.
- Keep Actions permissions read-only unless a workflow explicitly needs more.
- Do not use GitHub Pages; this application requires a Python server and writable SQLite runtime.

No license or `CODEOWNERS` file is assumed. Choose them deliberately.
