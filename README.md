# CCSync Local Scheduling Engine

This is the CCSync Python scheduling engine. The legacy demos use synthetic data;
the contract solve route consumes a submitted snapshot and does not connect to
Laravel, MySQL, Aiven, or any database. Deployment and Laravel environment settings
are documented in `docs/deployment.md`.

## Requirements

- Windows 11
- Python 3.12 (the project was initialized with Python 3.12.6)

## Create and activate the virtual environment

From the repository root in PowerShell:

```powershell
cd .\scheduling-engine
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use the Python executable directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The dependency versions are pinned in `requirements.txt`.

## Run the local API

```powershell
$env:SCHEDULER_API_KEY = "replace-with-a-long-random-value"
$env:SCHEDULER_PORT = "8000"
python -m ccsync_scheduler
```

`SCHEDULER_API_KEY` is required. `GET /health/live` is public; `GET
/health/ready`, validation-only `POST /api/v1/validate`, and synchronous `POST
/api/v1/solve` require the same value in the `X-CCSync-Key` header. If the key is
not configured, protected routes fail closed with HTTP 503; a missing or invalid
header returns HTTP 401. The Laravel client uses the same `SCHEDULER_API_KEY`
value from its environment.

`SCHEDULER_PORT` is optional and defaults to `8000`. The repository entry point
binds to `0.0.0.0`; Render supplies `PORT`, which takes precedence over
`SCHEDULER_PORT`. Network exposure must be controlled by the hosting environment.
The solve route validates a complete contract snapshot, runs CP-SAT, independently
validates feasible candidates, and returns a result without persisting or
publishing a timetable.

The application checks the CP-SAT runtime during startup and exits with an error
if it is unavailable. Uvicorn handles normal Ctrl+C shutdown and the application
logs startup and shutdown.

## Run tests and demos

```powershell
python -m pytest -q
python -m ccsync_scheduler.demo
python -m ccsync_scheduler.academic_demo
```

`ccsync_scheduler.demo` runs the original Milestone 1 example.
`ccsync_scheduler.academic_demo` runs the coordinated Milestone 2 timetable.

## Coordinated academic model

The Milestone 2 example creates one CP-SAT model for the entire synthetic week:
three programs, two year levels, four sections, seven subject offerings, and eight
required meetings. Every offering expands to its required weekly meeting frequency.
Separate offerings remain separate even when they use the same subject. Only an
explicit `CombinedClass` groups offerings into one shared meeting.

Each meeting selects one approved professor, one valid room, and a start time.
Hard constraints enforce professor, section, and room non-overlap; professor and
section availability; room capacity and exact room type; approved professor
assignments; and exactly-once scheduling. Multi-period meetings occupy consecutive
one-hour periods and cannot cross a day boundary. The independent validator checks
the complete assignment before the solver returns it.

The model uses weekdays Monday through Friday, indexed from zero, and one-hour
periods. Full-time professors are restricted to Monday through Wednesday unless
additional days are explicitly listed in `authorized_workday_exception_days`.
This is the synthetic interpretation of the supplied milestone rule, not a claim
that CCSync currently stores such exceptions. Part-time professors require
individual `weekly_target_periods`; deviation from the target is a soft objective.
No universal full-time workload quota is imposed.

The objective separately measures part-time target deviation, full-time load
balance, professor/section gaps, room-capacity waste, and per-section daily load
spread. The default `OptimizationWeights` are illustrative tuning values only,
not institutional policy. All hard constraints remain mandatory regardless of
objective scores. CP-SAT uses one search worker to keep the local prototype
lightweight.

Combined offerings in this prototype must use distinct sections and share a
subject, frequency, and room type. That is a synthetic modeling boundary to
confirm before using real CCSync data; offerings are never combined implicitly.

This deliberately small model does not read real CCSync records, persist drafts,
or implement an institutional workload policy. Its runtime is not a benchmark for
a full institutional timetable.
