# Manual Steps For CoastalDEM

Direct automated download is not implemented in this conservative first pass because CoastalDEM distribution is request-based and may involve additional approval or licensing steps.

Official product page:
- https://www.climatecentral.org/coastaldem-v2.1

What to do:
1. Open the product page and use the request workflow if access is needed.
2. Download the approved files manually.
3. Place them under `data/raw/coastaldem/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` to catalog them.
