# Web dashboard: HTTP API

`energy-analyzer serve` serves the dashboard (static files from `src/energy_analyzer/web/`) and
this JSON API from the same SQLite database the fetch jobs write. Everything is same-origin; the
UI never talks to anything else.

- Auth: HTTP Basic (`WEB_USER` / `WEB_PASSWORD` from `.env`) on every path when a password is
  set. Required whenever the server listens on a non-loopback address.
- Mutating requests (`POST`, `PUT`, `DELETE`) must send `Content-Type: application/json`
  (POST/PUT) and an `Origin` (or `Referer`) whose host equals the `Host` header, else `403`.
- Errors: status 4xx/5xx with `{"error": "message"}`.
- Dates are `YYYY-MM-DD` in the system's local time; powers in W, energies in kWh, ratios 0..1.
  Missing values are `null`.

## GET /api/meta
```json
{"version": "0.2.0", "system": "DEM*********0001", "first_day": "2025-06-01",
 "last_day": "2025-08-29", "last_fetch_at": "2025-08-29T10:15:02+00:00",
 "live_available": true, "demo": false,
 "settings": {"night_window": "02:00-05:00", "rolling_days": "28", "...": "..."},
 "event_kinds": [{"kind": "away", "label": "Holiday / away", "effect": "expect_lower",
                  "hint": "Nobody home: expect lower use, warn if it is not lower"}],
 "effects": ["expect_lower", "expect_higher", "regime_change", "ignore", "info"]}
```
`first_day`/`last_day` are `null` when the database is empty.

## GET /api/summary?from=YYYY-MM-DD&to=YYYY-MM-DD
Both optional; default = the last 30 days that have data.
```json
{"from": "...", "to": "...",
 "kpis": {"days": 30, "pv_kwh": 812.4, "load_kwh": 341.0, "grid_import_kwh": 12.1,
          "grid_export_kwh": 480.3, "self_consumption": 0.41, "self_sufficiency": 0.96,
          "baseline_w": 162.0, "baseline_trend_w_per_30d": 4.1,
          "anomalies": 5, "anomalies_unexpected": 3},
 "days": [{"day": "...", "baseline_w": 160.0, "night_max_w": 410.0, "pv_kwh": 30.1,
           "load_kwh": 11.2, "grid_import_kwh": 0.4, "grid_export_kwh": 17.9,
           "self_consumption": 0.41, "self_sufficiency": 0.96}],
 "anomalies": [{"day": "...", "kind": "baseline|consumption|night_spike|not_lower",
                "value": 426.0, "reference": 162.0, "score": 50.9, "message": "...",
                "expected_by": null}],
 "months": [{"month": "2025-06", "days": 30, "pv_kwh": 953.2, "load_kwh": 330.5,
             "grid_import_kwh": 0.0, "grid_export_kwh": 622.7, "self_consumption": 0.35,
             "self_sufficiency": 1.0, "baseline_median_w": 158.0}],
 "events": [EVENT, "..."]}
```
Units of `value`/`reference`: kWh for `consumption` and `not_lower`, W otherwise.

## GET /api/day?date=YYYY-MM-DD
Default date = last day with data.
```json
{"date": "...", "prev": "...|null", "next": "...|null",
 "night_window": {"start": "02:00", "end": "05:00"}, "baseline_w": 160.0,
 "samples": [{"t": "00:00", "pv": 0, "load": 171, "grid_import": 171, "grid_export": 0,
              "battery_charge": 0, "battery_discharge": 0, "soc": 52.1}],
 "totals": {"pv_kwh": 30.1, "load_kwh": 11.2, "grid_import_kwh": 0.4,
            "grid_export_kwh": 17.9, "battery_charge_kwh": 6.0,
            "battery_discharge_kwh": 5.1, "self_consumption": 0.41, "self_sufficiency": 0.96},
 "anomalies": [ANOMALY], "events": [EVENT]}
```
`totals` is `null` when the daily totals are missing; `samples` is `[]` without data.

## GET /api/heatmap?from=YYYY-MM-DD&to=YYYY-MM-DD
Default = the last 60 days with data. Mean household load per hour.
```json
{"from": "...", "to": "...", "days": ["...", "..."], "hours": [0, 1, "...", 23],
 "values": [[171, 165, "...", 420], "..."], "max": 4210}
```
`values[i][h]` belongs to `days[i]`, hour `h`; `null` where there are no samples.

## GET /api/live
Latest values from the AlphaESS API (`getLastPowerData`), cached for 60 s.
```json
{"available": true, "fetched_at": "2025-08-29T10:15:02+00:00", "pv_w": 2310,
 "load_w": 640, "grid_w": 0, "battery_w": -1670, "soc": 72.5}
```
`{"available": false, "error": "..."}` when credentials are missing or the API fails.
Signs as delivered by AlphaESS: `grid_w` > 0 = import, `battery_w` < 0 = charging.

## Events
`EVENT` = `{"id": 3, "kind": "away", "title": "Summer holiday", "start_day": "2025-08-01",
"end_day": "2025-08-14", "effect": "expect_lower", "note": ""}`. Events with effect
`regime_change` have `end_day: null` (they mark the day a new pattern starts).

- `GET /api/events` → `{"events": [EVENT, ...]}`
- `POST /api/events` body `{"kind", "title"?, "start_day", "end_day"?, "effect"?, "note"?}` →
  `201 {"event": EVENT}`; `title` and `effect` default to the kind's label and effect.
- `PUT /api/events/<id>` same body → `200 {"event": EVENT}`
- `DELETE /api/events/<id>` → `204`

Event effects on the analysis are described in `src/energy_analyzer/events.py`.

## GET /report.html
The classic single-file report for the whole database (or `?from=&to=`).
