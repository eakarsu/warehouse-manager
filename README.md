# Warehouse Manager App

Industry: **Transportation & Logistics**  
Specialization: **Warehouse Manager**

This standalone application consolidates source-backed workflows into 303 optimized features, including 193 data-backed or AI-enabled views. Its public demo SQLite database contains 0 sanitized seed rows across 0 imported tables.

## Run locally

Python 3.12 or newer is recommended. No third-party packages are required.

```bash
cp .env.example .env
./start.sh
```

The server listens on `127.0.0.1:4400` by default. OpenRouter is optional and is used only by explicitly labeled AI actions.

## Validate

```bash
python scripts/validate_app.py
python scripts/smoke_test.py
```

## Public demo data

The committed database is a sanitized public demo. Saved AI runs are removed, credential/contact fields are pseudonymized, and local machine paths are normalized. Do not use the development server or sample authentication as production security controls.

## Source provenance

Source repository names and evidence paths are retained as provenance metadata; local source checkouts and their environment files are not included.

- `AIAerospaceMRO`
- `AIFreightPricingAgent`
- `AISatelliteImageryAnalyzer`
- `AIWarehouseManager`
