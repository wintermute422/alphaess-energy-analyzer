# Contributing

Thanks for helping! Bug reports, fixes and new analyses are welcome.

## Setup

```bash
git clone https://github.com/wintermute422/alphaess-energy-analyzer.git
cd alphaess-energy-analyzer
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
git config core.hooksPath .githooks   # enables the secret-scanning git hooks
bash scripts/check.sh                 # the same gate CI runs (also: make check)
```

`scripts/check.sh` runs ruff (lint + format), mypy (strict), pytest, a secret scan and the
selftest. The git hooks need [gitleaks](https://github.com/gitleaks/gitleaks) on your PATH or
Docker (the pinned gitleaks image is used automatically).

## Rules

- **Never commit real data or credentials.** Tests and `sample_data/` use synthetic data only
  (`python -m energy_analyzer.synthetic --out sample_data --days 3` regenerates the fixtures).
  A real day of your consumption shows when you are at home – keep it out of issues too.
- Nothing author- or site-specific in code or docs: no serials, hostnames, IPs, home paths,
  tariffs. Everything configurable goes through `config.toml`, `.env` or CLI flags.
- No live API calls in tests; use `fake_api.SyntheticTransport` or the fixture files.
- Runtime code uses only the Python standard library (Python 3.10+).
- Keep the report offline: no CDN, no external fonts, scripts or images.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), one topic per PR,
  tests for new behaviour. Do not lower thresholds or delete tests to make CI pass.

## Pull requests

Open a PR against `main` from a feature branch. CI (lint, types, tests on 3.10–3.13, selftest,
gitleaks over the full history, Docker build) must be green before review.
