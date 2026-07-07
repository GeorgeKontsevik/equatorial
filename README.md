# equatorial

Equatorial road-surface, precipitation, and crop-accessibility experiments.

## Scheme

```mermaid
flowchart LR
    A[Inputs] --> B[Run: scripts/fetch_equator_700km_full_year_data.sh]
    B --> C[Checked outputs]
    C --> D[Paper / thesis use]
```

## Main Result

![Main result](equator_country_belt_map_700km.png)

## Run

Entrypoint: `scripts/fetch_equator_700km_full_year_data.sh`

Human:

```bash
bash scripts/fetch_equator_700km_full_year_data.sh
```

Agent:

After any run inspect overlay outputs and weekly summary tables; do not treat fetch success as analysis success.

## Publication

See `Research Compilation I.pdf` and thesis publication bundle.

## Next Steps / Heuristics

Heuristic: precipitation-only ERA5 path is current production path; flood depth remains deferred unless real depth data exists.
