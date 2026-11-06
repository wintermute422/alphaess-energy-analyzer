# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") for this repository. You will get an answer within
a few days; this is a volunteer project without an SLA.

## Your credentials

- Your AlphaESS Developer Key belongs in `.env` only (git-ignored, `chmod 600`). Never paste it
  into issues, logs or screenshots.
- If you think your key leaked, rotate it in your developer account on open.alphaess.com.
- The tool never logs the key and masks system serials in its output; please report any place
  where that is not the case.

## Supported versions

Only the latest release receives fixes.
