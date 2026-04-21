# Manual Steps For FLOPROS

Automatic download of the official FLOPROS supplement archive did not succeed in the current environment.

Primary reference:
- https://nhess.copernicus.org/articles/16/1049/2016/nhess-16-1049-2016.html

Supplement archive:
- https://nhess.copernicus.org/articles/16/1049/2016/nhess-16-1049-2016-supplement.zip

What to do:
1. Download the official supplement ZIP archive.
2. Place it under `data/raw/flopros/global/`.
3. Extract it under `data/raw/flopros/global/original/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
