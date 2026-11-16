
# ASTRA PULSE

ASTRA PULSE is an astronaut health-monitoring system that evaluates crew health indicators using rule-based monitoring and compound health rules.

## Getting Started

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Start the backend:

```bash
python astra_backend.py serve
```

Open the application at:

**http://127.0.0.1:8000/ui/**

## Dataset and Indicators

This version uses rules, containing 20 indicators:

- 16 crew indicators
- 1 motion sickness indicator
- 3 body temperature indicators
- 6 compound rules

The dataset contains 30 days of readings for each crew member:

- `A01_30_days.csv`
- `A02_30_days.csv`
- `A03_30_days.csv`

Each CSV contains 20 indicators plus `posture`. The columns are named after the rule indicator IDs and load automatically.

The database (`astra.db`) is already populated. To reload the data, run:

```bash
python astra_backend.py load A01_30_days.csv --reset
python astra_backend.py load A02_30_days.csv
python astra_backend.py load A03_30_days.csv
```

Each crew member has 600 readings.

## Frontend

The frontend is located at:

```text
astra-frontend/index.html
```

The questionnaire has 7 steps covering the 20 rule indicators, plus an app-only pain question. Body areas on the map follow the indicators: Mind and cabin air, Heart and breathing, Temperature, Motion sickness and Exercise.

Available crew IDs are `A01`, `A02`, and `A03`.

**Dashboard.** Tapping a crew card shows that astronaut's own saved data (latest saved day, scores, last 7 days, sleep and fatigue chart). It reads `/subjects/<id>/status` and `/subjects/<id>/history`.

**Mission day and phase are dynamic.** The next check is the day after the astronaut's last saved day, in the same phase as that day. The phase can be changed on the measurement screen (for example from inflight to postflight). `?day=&phase=` in the URL still overrides both.

**"Not scored yet".** Personal-baseline checks need 3 earlier days in the same phase and posture. Right after a phase change, readings show "Not scored yet" and the dashboard explains why. Fixed NASA limits still apply during that time.

## Checking the rules against the data

Run the replay for each astronaut to see how often they are flagged:

```bash
python astra_backend.py replay A01
python astra_backend.py replay A02
python astra_backend.py replay A03
```

Result with the current CSVs (30 days each): A01 has 8 action days, A02 has 4, A03 has 17 (short sleep). Nobody is flagged every day or never. Action days come from the NASA-cited 7-day sleep and 3-of-5-day circadian rules. The CSV `cabin_co2_mmHg` values were lowered by 1.2 because they sat above NASA's 3 mmHg limit on every day, which flagged all three astronauts all the time.

## Compound Rules

Compound rules can use readings from another group for the same crew member and day.

For example:
- CO2 is in `body_temperature`.
- Headache is in `crew`.
- Heat strain uses crew aerobic minutes.

However, baselines and reference values are never pooled across groups.

