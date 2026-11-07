# Follow the Money

A free public-interest site that shows what federal candidates raise and spend, using public FEC filings, and who pays to lobby Congress, using Lobbying Disclosure Act reports from lda.gov.

- `index.html` is the site.
- `data/candidates.json` is the data the site reads.
- `scripts/sync_fec.py` pulls fresh data from OpenFEC.
- `.github/workflows/sync.yml` runs the sync every day.
- `data/lobbying.json` holds lobbying firms, their clients and their lobbyists for the last four reported quarters.
- `scripts/sync_lda.py` builds it from lda.gov, and `.github/workflows/lobbying.yml` runs it every day. It needs no key: the first download takes about four hours, the reports are cached between runs, and later runs only fetch new ones. An optional `LDA_API_KEY` secret makes the first download faster.

The FEC sync needs a repository secret named `FEC_API_KEY`. Never commit a key.

Individual donors are deliberately not stored or shown.
