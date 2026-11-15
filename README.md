## To run

```
pip install -r requirements.txt
python astra_backend.py serve
```
it will run on: http://127.0.0.1:8000/ui/

## 20-indicator version (rules v5.1.0-compact20)
- `astra_rules.json`: crew (16) + motion_sickness (1) + body_temperature (3) = 20 indicators, 6 compound rules.
- `A01/A02/A03_30_days.csv`: 30 days x 20 indicators + `posture`. Columns are named after the rule indicator ids and load automatically:
  `python astra_backend.py load A01_30_days.csv --reset` then `load A02_30_days.csv`, `load A03_30_days.csv` (600 readings each). `astra.db` is already loaded.
- Compound rules can use another group's reading for the same person and day (CO2 is in body_temperature, headache in crew; heat strain uses crew aerobic minutes). Baselines and references are still never pooled across groups.
- `astra-frontend/index.html`: the questionnaire asks exactly these indicators (plus glucose and pain, which are app-only), crew ids are A01-A03, default check is day 31 (`?day=&phase=` to change).
- Tests: `pytest` (the API test needs `pip install -r requirements.txt`).
