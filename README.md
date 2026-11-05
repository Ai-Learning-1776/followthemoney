# Follow the Money

A free public-interest site that shows what federal candidates raise and spend, using public FEC filings.

- `index.html` is the site.
- `data/candidates.json` is the data the site reads.
- `scripts/sync_fec.py` pulls fresh data from OpenFEC.
- `.github/workflows/sync.yml` runs the sync every day.

The sync needs a repository secret named `FEC_API_KEY`. Never commit the key.

Individual donors are deliberately not stored or shown.
