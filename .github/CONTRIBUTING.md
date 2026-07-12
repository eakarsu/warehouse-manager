# Contributing

Use Python 3.12 or newer. Copy `.env.example` to `.env` only when local configuration is needed, then run:

```bash
./start.sh
```

Before opening a pull request:

```bash
python scripts/validate_app.py
python scripts/smoke_test.py
```

Keep ordinary features deterministic and available without `OPENROUTER_API_KEY`. AI calls must be clearly labeled, explicitly initiated by the user, and treated as data sent to an external processor. Never commit credentials, local source links, runtime sidecars, or sensitive data.
