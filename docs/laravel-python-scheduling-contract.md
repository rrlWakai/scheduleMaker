# CCSync Laravel-Python Scheduling Contract

**Status: implemented contract; production deployment and lifecycle limits are
documented below.** Contract v1.0 request/response models, deterministic semantic
validation, `POST /api/v1/validate`, `POST /api/v1/solve`, the minute-based CP-SAT
adapter, Laravel HTTP integration, response validation, candidate persistence,
and candidate revision history are implemented. Laravel-controlled approval and
atomic publication remain outside this contract and are identified as deferred.

**Revision (Milestone 4.2):** incorporates the approved institutional scheduling
rules: semester-based generation and recurrence, manual mid-semester changes,
professor assignments across year levels and departments, one professor per
ordinary scheduled meeting in version 1, and the CCS-specific minor-subject and
specialization rule. These rules are labeled **APPROVED** below. Nothing in this
revision changes any previously approved technical decision.

**Revision (Milestone 4.3):** final corrections — exactly one distinct
Dean-approved professor per ordinary subject offering in version 1 (with
Laravel-side ambiguity rejection for multiple distinct eligible professors),
complete-semester offering coverage with pre-send validation and diagnostics,
and removal of outdated wording about the instructor-count limitation. See
section 13.

**Revision (Milestone 4.4):** full-semester re-optimization — every required
offering is solved into a complete candidate; the current published timetable
is a replaceable baseline, not a set of fixed solver commitments. Candidate
review and approval precede an atomic Laravel-controlled publication, with the
previous timetable retained as history. See sections 2, 4.9–4.10, and 9.

**Revision (Milestone 4.5):** adds a configurable soft objective to minimize
changes from reliably matched published placements. It prefers the previous
day, start time, and room when valid; it never relaxes hard constraints or
changes pre-assigned professors. No institutional weight/default is approved.
Placement-retention metrics are reported only for baseline meetings that can be
matched deterministically. See sections 4.10, 4.11, 5, and 6.

**Revision (Milestone 5.2):** defines strict Python request/response models and
deterministic semantic validation. The implemented service also provides the
synchronous `POST /api/v1/solve` route and the integer-minute CP-SAT adapter.

## Contract v1.0 capabilities and transport

- `POST /api/v1/solve` and `POST /api/v1/validate` require `Content-Type:
  application/json` and the `X-CCSync-Key` header. `GET /health/ready` also
  requires `X-CCSync-Key`; `GET /health/live` is public.
- `SCHEDULER_API_KEY` is required by Python. A missing Python key fails closed
  with HTTP `503 scheduler_auth_not_configured`; a missing or invalid header
  returns HTTP `401 invalid_api_key`.
- Supported weekdays are Monday through Friday, encoded as `1..5`. Weekend days
  and `day_of_week` exceptions are unsupported in v1.0.
- Request bodies are limited to 10 MiB. `solver_config.time_limit_ms` is a
  positive integer capped at `300000`.
- Solver statuses are `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, and `UNKNOWN`.
  Result statuses are `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`, and `TIME_LIMIT`.
- The implemented contract supports approved time grids, meeting plans,
  full-time workday policies, professor availability, part-time targets,
  published baselines, deterministic placement-retention metrics, optional
  meeting omission, and preferred-window misses.
- Unsupported in v1.0: weekend days, `day_of_week` exceptions, `time_slot_id`,
  `slot_id`, `objective_breakdown`, `feasibility_explanations`, `unsat_core`,
  section-specific availability values, combined classes, fixed schedule IDs,
  replacement draft selection, and external commitments. Laravel sends empty
  values for unsupported collections where the schema requires a list.
- Python is stateless and never writes Laravel or scheduling database records.

## 1. Purpose and architecture

Laravel is the authority for authentication, authorization, business rules, source
records, generation records, database transactions, draft persistence, approval, and
publication. Python receives an immutable scheduling snapshot, solves it, validates
its candidate independently, and returns a result. Python never receives database
credentials and never connects to MySQL or writes CCSync scheduling tables.

The Python service exposes `GET /health/live`, authenticated `GET /health/ready`,
authenticated `POST /api/v1/validate`, and authenticated `POST /api/v1/solve`.
Laravel exposes its own scheduling endpoints and calls the Python solve endpoint
through `SchedulerClient`; browser clients never call Python directly.

The solve call is synchronous: one request contains the complete snapshot and
solver settings, and one response contains the outcome. Laravel owns generation
records, persistence, review, and publication. No Python broker, worker, polling
API, or cancellation API is part of this contract.

### Development and deployed environments

- **Local Laravel + local Python:** run both applications on the developer's
  machine. Laravel calls `http://127.0.0.1:<port>` using `SCHEDULER_URL`; the
  Python launcher binds to `0.0.0.0` and accepts `SCHEDULER_PORT` as the local
  port default. Browser frontends continue to call Laravel, not Python.
- **Deployed Laravel + deployed Python:** the supported production arrangement.
  Render provides `PORT` to the Python web service and Laravel receives the
  deployed service URL through `SCHEDULER_URL`. Both services must use the same
  secret `SCHEDULER_API_KEY`. See `docs/deployment.md` for the manual Render
  service settings that preserve the existing deployed service.

The solve endpoint, service connection, and authenticated readiness checks are
implemented. `GET /health/live` is public; all protected routes fail closed when
the Python API key is missing.

### Evidence labels

- **VERIFIED** means confirmed in the current repository source.
- **APPROVED** means a business decision confirmed for this milestone.
- **PROPOSED** means a contract decision that is not implemented unless the
  surrounding text says otherwise.
- **IMPLEMENTED** means the behavior exists in the current repositories.
- **DEFERRED** means future application/database work.
- **UNKNOWN** means institutional or technical input still needs confirmation.

## 2. Current implementation boundary

### Verified Laravel behavior

- Laravel 12 exposes the API under `/api/v1`, uses Sanctum and role/policy checks,
  and owns the database.
- Relevant models connect terms, sections, programs, year levels, subjects,
  offerings, professors, faculty assignments and availability, rooms, schedules,
  schedule assignments, room allocations, and schedule conflicts.
- Schedule creation accepts a term offering, weekday `1..7`, `H:i` start/end,
  `lecture|laboratory|seminar|other` schedule type, and
  `face_to_face|online|hybrid` modality. The service creates schedules as `draft`.
- A schedule can be published only when it has at least one active instructor and
  exactly one primary instructor. Each scheduled instructor must have an active
  assignment to that offering and an active professor profile and user account.
- Laravel permits multiple `schedule_assignments` on a schedule, each referencing
  an offering-level `faculty_assignment` and carrying its own `is_primary` flag;
  publication requires exactly one primary. Assignment roles do not currently
  enforce a lecture/laboratory-to-instructor mapping.
- Face-to-face and hybrid schedules require exactly one active room allocation;
  online schedules cannot have a physical allocation. An allocated room must have
  `available` status and enough capacity for the offering's non-null
  `expected_students`.
- Existing conflict checks use half-open interval comparisons: schedules touching
  at an endpoint do not overlap. Publication checks published schedules for
  section, professor, and active-room conflicts.
- Faculty availability is term-scoped, uses weekday `1..7`, `H:i` windows, and
  `available|unavailable|preferred` types. Windows must have `end_time` after
  `start_time`; overlapping entries are rejected. Published-schedule checks reject
  overlap with unavailable windows. If one or more `available` or `preferred`
  windows exist on that day, a meeting must fit wholly inside one such window.
  `preferred` currently has no separate optimization behavior.
- Assignment roles are `lead|co_instructor|lab_instructor|substitute`; active
  assignment status, not the role name, is the current eligibility check. The
  current publishing rule does not infer primary status from assignment role.
- Room types are `lecture|laboratory|seminar|office|other`; room statuses are
  `available|unavailable|maintenance|retired`.
- `expected_students` is nullable. Subject catalog `lecture_hours` and `lab_hours`
  are descriptive decimals; offering-level meeting plans define meeting frequency,
  duration, modality, room type, allowed days, valid starts, and mandatory status.
- Laravel stores approved term-scoped time grids, meeting plans, part-time weekly
  targets, and term-versioned full-time workday policies with exceptions. It also
  stores generation records, immutable snapshots, candidates, and candidate
  revisions. Section availability, combined classes, fixed schedule IDs,
  replacement draft selection, and external commitments remain unsupported and
  are sent as empty collections in v1.0.

### Verified Python behavior

- The Python service uses Python 3.12, FastAPI, Pydantic, and OR-Tools CP-SAT. It
  exposes public liveness, authenticated readiness, authenticated validation, and
  authenticated solve routes.
- The contract adapter uses integer minutes within explicit approved operating
  hours and valid start boundaries. It does not use the legacy zero-based
  `TimeSlot(day, period)` model for contract v1.0.
- The adapter requires explicit meeting duration, modality, room type, and an
  approved professor per meeting. It enforces mandatory meeting occurrences,
  resource non-overlap, availability, capacity, room type, and full-time workday
  policy plus authorized exceptions.
- Part-time targets are integer `part_time_weekly_target_minutes` and contribute a
  soft absolute-deviation objective. Solver statuses are `OPTIMAL`, `FEASIBLE`,
  `INFEASIBLE`, and `UNKNOWN`; result statuses are `FEASIBLE`, `INFEASIBLE`,
  `UNKNOWN`, and `TIME_LIMIT`. The independent contract validator checks returned
  candidates.
- Legacy prototype combined classes are separate from contract v1.0. Contract
  v1.0 accepts `combined_classes` in the schema but Laravel sends an empty list
  because the supporting workflow is unsupported.
- The repository tests cover strict contract validation, the HTTP routes, the
  minute-based CP-SAT adapter, independent result validation, and solver behavior.

### Approved institutional scheduling rules

The following business rules are **APPROVED** for this contract. They describe
approved behavior; implementation status and remaining gaps are stated inline.

1. **Semester-based scheduling (APPROVED).** AI generates the timetable for an
   entire academic semester (one Laravel academic term). Generation coordinates
   all configured programs, year levels, sections, subject offerings,
   professors, and rooms. The generated timetable recurs weekly throughout the
   applicable semester. A new semester requires a new semester timetable.
   Previous semester schedules remain preserved as historical records. Daily
    schedule generation is not the normal workflow. Laravel builds a
    term-scoped generation payload and persists generation and snapshot records;
    whole-semester coverage is enforced by Laravel pre-send readiness checks.
2. **Manual mid-semester changes (APPROVED).** Authorized personnel may manually
   modify affected schedules during an active semester using the existing
   Laravel draft/publish/cancel workflow. A manual change must not automatically
   trigger regeneration of the entire semester timetable. Unaffected schedules
   are preserved. Conflict and validation handling uses existing Laravel
    capabilities (publication checks, conflict records, resolve/waive). No new
    approval roles or permissions are introduced. Laravel records generation
    scope and snapshots, but draft-vs-draft overlap remains unsupported and full
    payload revalidation after a solver round trip is not atomic.
3. **Professor assignments across year levels and departments (APPROVED).** A
   professor may teach different subjects across multiple year levels and may
   teach subjects belonging to another department, including minor subjects
   handled by professors from other departments. A professor's home department
   must not automatically restrict what they can teach, and the subject's owning
   department must not automatically determine professor eligibility. The Dean
   or authorized academic personnel determine which professor is assigned to
   each subject offering. In scheduling version 1, each ordinary subject
   offering (subject + section + academic term) has one distinct Dean-approved
   professor; the AI must never choose between multiple professors for the same
   ordinary offering or redistribute professors between sections — it determines
   the time and room for the already assigned professor (section 4.6). The
   Python scheduler must respect those approved assignments. Different sections
   taking the same subject may have different professors when assigned that
   way. A professor may handle multiple subjects and year levels, subject to
   approved assignments, availability, and workload constraints.
   *Gap:* the contract's `eligible_for_scheduling` derivation and the current
   Laravel publish check already ignore department matching; no code change is
    required for this rule, but the "Dean" terminology is not mapped to an
    existing role (see section 12). Laravel readiness rejects multiple distinct
    eligible professors on one offering before a solve request is built.
4. **Instructor count per meeting (APPROVED).** Version 1 supports one professor
   per ordinary scheduled meeting. Co-instructors teaching the same section and
   meeting are not part of the current institutional scheduling requirements.
   Different offerings of the same subject may have different assigned
   professors. Assigning professors across different sections is distinct from
   assigning multiple professors to one meeting; the latter is not implemented
   for version 1. This is approved (Milestones 4.2/4.3) and no longer requires
   approval (section 4.6).
5. **CCS minor subjects and specialization (APPROVED, CCS-specific).** For the
   College of Computer Studies, minor subjects apply to 1st- and 2nd-year
   students. Third-year students choose the major or specialization they intend
   to pursue. The curriculum and subject offerings determine which subjects are
   required for each year level and selected major. Minor subjects must not be
   automatically added to every section merely because it is 1st or 2nd year.
   Not all programs or departments follow CCS curriculum rules; this is a
   CCS-specific academic curriculum rule, not a universal scheduling rule.
    Fourth-year scheduling follows the applicable curriculum and selected major
    requirements. Laravel stores active curriculum requirement mappings and uses
    them in readiness checks; the authoritative student specialization source and
    end-to-end curriculum authoring workflow remain unresolved.
6. **Full-semester AI re-optimization (APPROVED, Milestone 4.4).** A generation
   covers every required offering across all configured programs, year levels,
   and sections. The current published timetable is included as a baseline but
   is not automatically a fixed solver commitment; the solver may propose
   different days, times, rooms, and arrangements. Professor assignments remain
   fixed to their Dean-approved offering assignments, and all hard constraints
   remain enforced. An incomplete or infeasible result is never presented as a
   complete candidate. The active timetable remains unchanged through generation,
   review, and rejection; only authorized approval and publication may replace
    it. Laravel persists immutable candidates and candidate revisions; approval
    authorization mapping and atomic timetable publication remain deferred.
7. **Minimal timetable change (APPROVED, Milestone 4.5).** As a soft objective,
   prefer retaining a reliably matched published meeting's previous day, start
   time, and room when that placement remains valid. The penalty is configurable;
   no institutional weight or default is approved. Hard constraints always take
   priority, and professor assignments remain fixed to their Dean-approved
    offering assignments. Retained/changed counts are reported only where the
    baseline can be matched deterministically. The v1.0 adapter implements this
    soft objective; historical schedules without stable occurrence linkage cannot
    be compared reliably.

## 3. Contract versioning, identifiers, and time conventions

### 3.1 Version and IDs

- `contract_version` is a string in `major.minor` form. This initial proposal is
  `"1.0"`. A service must reject an unsupported major version. Additive optional
  fields may be introduced in a compatible minor version; a consumer must reject
  unknown enum values and must not silently reinterpret required fields.
- All Laravel `BIGINT` identifiers are decimal **strings**, not JSON numbers, to
  avoid precision loss in JavaScript clients. UUIDs are canonical lowercase UUID
  strings.
- IDs are opaque identifiers. Python must not infer relationships from their
  numeric values or from display codes.

### 3.2 Snapshot metadata and hash

Laravel creates `generation_id`, `requested_at`, `academic_term_id`,
`snapshot_version`, `snapshot_sha256`, and (optionally) `correlation_id`.

- `generation_id`: UUID unique to a generation record.
- `requested_at`: RFC 3339 UTC timestamp ending in `Z`.
- `academic_term_id`: must equal `academic_term.id` in the input.
- `snapshot_version`: positive integer, monotonically increasing for distinct
  snapshots of the same term; retries of the identical snapshot keep the same
  version. This requires a future Laravel generation/revision ledger.
- `snapshot_sha256`: lowercase 64-character SHA-256 of the UTF-8 RFC 8785 JSON
  Canonicalization Scheme serialization of the complete request object after
  removing `contract_version` and `metadata`. The hashed object includes the
  academic data, complete-semester offering set, published timetable baseline,
  generation scope, explicit protected commitments, and solver configuration.
  Validate the digest before solving.
- `correlation_id`: optional UUID used to link Laravel logs, request, and response.
  It is not an authorization credential.

### 3.3 Weekday, clock, and interval

- Contract v1.0 wire weekday numbers are `1=Monday` through `5=Friday`. Laravel
  may store `1..7` for other workflows, but the scheduling provider rejects
  weekend days before building a v1.0 request. Python uses `Weekday = 1..5`
  directly and does not apply the legacy zero-based `TimeSlot.day` adapter.
- Times are local wall-clock strings in strict 24-hour `HH:mm` form (`00:00` to
  `23:59`), interpreted using `time_grid.timezone`, an IANA timezone name.
  Seconds and date offsets are not permitted.
- Intervals are half-open: `[start, end)`. Thus `09:00-10:00` and `10:00-11:00`
  may be back-to-back. `end` must be later than `start` on the same day.
- A meeting occupies `duration_minutes` from its start. The end is computed as
  `start + duration_minutes`; the end need not be a valid start boundary. No meeting
  may cross the operating-day end or midnight.
- `24:00` is not a wire value. If an institution needs an exclusive closing time
  of midnight, the contract must be extended rather than interpreting `00:00`
  differently for selected fields.
- A weekday in `academic_term.applicable_days` is a weekly recurrence: it must
  occur at least once within the inclusive term dates. This does not encode
  holidays, closures, date-specific exceptions, or one-off sessions.
- A fixed uniform period index is not the proposed wire representation. Integer
  minutes since local midnight preserve exact `H:i` windows and different meeting
  durations without rounding. `valid_start_times` restrict candidate starts;
  actual intervals are then computed from the exact duration. This also handles
  availability that starts or ends between institutional start boundaries.

## 4. Request contract

The request is a strict JSON object. Unknown fields are rejected for a given
`contract_version`; required keys may not be omitted or set to `null` unless noted.
The structure below is normative; the example values are illustrative.

```json
{
  "contract_version": "1.0",
  "metadata": {
    "generation_id": "a11ce000-1111-4111-8111-111111111111",
    "requested_at": "2026-01-15T10:00:00Z",
    "academic_term_id": "42",
    "snapshot_version": 1,
    "snapshot_sha256": "64-lowercase-hex-characters",
    "correlation_id": "b22ce000-2222-4222-8222-222222222222"
  },
  "academic_term": {},
  "time_grid": {},
  "generation_scope": {},
  "published_timetable_baseline": [],
  "programs": [],
  "year_levels": [],
  "sections": [],
  "subjects": [],
  "offerings": [],
  "professors": [],
  "professor_assignments": [],
  "full_time_workday_policy": {},
  "rooms": [],
  "combined_classes": [],
  "existing_commitments": [],
  "solver_config": {}
}
```

### 4.1 Metadata and academic term

`academic_term`:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id` | string | yes | Same as `metadata.academic_term_id`. |
| `academic_year` | string | yes | Laravel format `YYYY-YYYY`; this does not define dates. |
| `term` | string | yes | Current Laravel values: `first`, `second`, `summer`. |
| `name` | string | yes | Display name; not used as a scheduling constraint. |
| `starts_on`, `ends_on` | date strings | yes | ISO `YYYY-MM-DD`; inclusive; end not before start. |
| `status` | string | yes | Must be `active` or `planned` for this request. |
| `applicable_days` | integer array | yes | Nonempty, unique, sorted weekdays `1..5`; must match the days in `time_grid`. Each must occur during the term. |

Term ID/status and date range are existing data. The applicable scheduling days
and their approved operating hours require institutional configuration; they are
not inferred from the term name or the subject catalog.

### 4.2 Time grid

```json
{
  "configuration_id": "grid-term-42",
  "version": 1,
  "academic_term_id": "42",
  "approval_status": "approved",
  "approved_at": "2026-01-10T09:00:00Z",
  "timezone": "Asia/Manila",
  "days": [
    {
      "day_of_week": 1,
      "operating_start": "08:00",
      "operating_end": "17:00",
      "valid_start_times": ["08:00", "08:30", "09:00", "09:30"]
    }
  ]
}
```

| Field | Type | Required | Rule |
|---|---|---:|---|
| `configuration_id` | string | yes | Stable ID of the approved institutional time-grid configuration stored by Laravel. |
| `version` | positive integer | yes | Immutable version for this academic term. |
| `academic_term_id` | string | yes | Must match the request term. |
| `approval_status` | string | yes | Must be `approved`; Laravel sends no draft configuration. |
| `approved_at` | RFC 3339 UTC timestamp | yes | Approval time for this version; approval actor is retained in Laravel audit data, not assigned a new role by this contract. |
| `timezone` | IANA timezone string | yes | Must be an installed, recognized timezone from the approved Laravel time grid. |
| `days` | array | yes | One entry per `applicable_days` value, no duplicates, sorted by weekday. |
| `day_of_week` | integer | yes | ISO weekday `1..5`; weekend days are unsupported in v1.0. |
| `operating_start`, `operating_end` | `HH:mm` | yes | `operating_start < operating_end`. |
| `valid_start_times` | `HH:mm` array | yes | Nonempty, unique, sorted; each start is at or after open and before close. |

The solver converts each time to `hour * 60 + minute`; it does not round to a
period. Each chosen meeting must start at one of the applicable valid boundaries
and finish no later than `operating_end`. A meeting-specific boundary list, when
provided, must be a subset of the applicable day's global list.

#### Institutional time-grid ownership

**IMPLEMENTED:** Laravel stores and administers a versioned institutional
configuration for each academic term. That configuration owns the timezone,
operating days, daily operating hours, and valid scheduling start boundaries.
Creating or changing it requires an explicit institutional approval and creates a
new version; a generation references one approved version, and its snapshot keeps
the resolved values immutable. Configuration changes do not silently alter an
existing generation or an approved/published schedule.

The authorized institutional owner and approver are **not decided**. This document
does not assign that responsibility to an existing Laravel role. Whether the
configuration is institution-wide or varies by campus/program also needs
confirmation; this contract represents one approved configuration per term.

Time-grid records, approval/version history, and time-grid fields are business
policy data and remain separate from `solver_config`, which contains technical
CP-SAT limits and objective weights.

### 4.3 Programs, year levels, and sections

`programs[]` fields: `id` string, `code` string, `name` string, `status` string.
Only active programs may be included.

`year_levels[]` fields: `id` string, `level` positive integer, `name` string.

`sections[]` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id` | string | yes | Unique section ID. |
| `program_id`, `year_level_id`, `academic_term_id` | strings | yes | Must reference included entities and the request term. |
| `code`, `name` | strings | yes | Descriptive values. |
| `capacity` | integer or `null` | yes | Existing section capacity if known; informational until the institution confirms it is a solver constraint. |
| `status` | string | yes | Must be `active`. |
| `availability_windows` | array | yes | Defaults to `[]`; section constraint unsupported by Laravel v1.0. |

A section availability item is `{ "day_of_week": 1, "start_time": "08:00",
"end_time": "12:00", "availability_type": "available" }`. If the list is empty,
the section has no additional availability restriction. Otherwise each meeting
must fit wholly inside one window on that day; windows are not merged. Section
availability is unsupported in Laravel v1.0, so the provider sends `[]`.

Section capacity is included for traceability. It does not itself block a
schedule in version 1 because the current source does not establish whether it
is an enrollment limit, room-size substitute, or informational value.

### 4.4 Subjects and offerings

`subjects[]` fields: `id`, `code`, `title`, `status`, `units`, `lecture_hours`,
`lab_hours`. IDs and text are strings; decimal catalog values are decimal strings
(for example `"3.0"`) or `null` where the source permits null. Only active
subjects are included. Catalog hours are descriptive only: the solver must never
derive a meeting count or duration from them.

`offerings[]` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id`, `academic_term_id`, `section_id`, `subject_id` | strings | yes | References must resolve; term and section terms must match the request term. |
| `expected_enrollment` | integer or `null` | yes | Nonnegative. Must be non-null for any physical-room meeting or explicit combined meeting. Laravel must not silently guess when `expected_students` is null. |
| `status` | string | yes | Must be `open`. |
| `meeting_plan_id` | string | yes | Stable ID of the approved offering-level plan stored by Laravel. |
| `meeting_plan_version` | positive integer | yes | Approved immutable plan version used by this snapshot. |
| `meeting_plan_approval_status` | string | yes | Must be `approved`; Python does not approve plans. |
| `meeting_plan_approved_at` | RFC 3339 UTC timestamp | yes | Approval time; approving actor/audit record remains Laravel-owned. |
| `meeting_requirements` | array | yes | At least one explicit requirement; no fallback to catalog hours. |

An offering is the current Laravel relation of one subject, one section, and one
academic term. It is not a meeting. Every included offering must have a complete,
approved meeting plan before a generation request can be sent, and exactly one
distinct approved professor assignment (section 4.6). In a whole-semester
generation the offering set must be the complete required term set (section
4.9, "Complete-semester offering coverage"): an offering that fails these
prerequisites causes the generation attempt to be rejected with diagnostics, not
silently excluded.

#### Proposed meeting-plan workflow and ownership

The subject catalog's `lecture_hours` and `lab_hours` are catalog-level values.
They may inform academic planning, but they do not determine an offering's weekly
frequency, each meeting's actual duration, meeting type, modality, room type, or
how catalog hours are distributed. Those are explicit offering-level requirements.

**IMPLEMENTED workflow:** an offering plan is drafted for a specific term,
reviewed and approved, then versioned and locked for snapshot use. A change
creates a new plan version; it does not mutate prior generation snapshots. Laravel
records the offering/term, requirement IDs, approval status, version, approval
timestamp, and the approving actor in its audit history. Only `approved` plan
versions are sent to Python. Python validates the status and relationships but
does not confer academic approval.

The authoring and approval roles, review sequence, and whether the same person may
author and approve are **UNKNOWN**. No existing Laravel role is assigned that
responsibility by this contract. The plan fields, approval status, and versioned
storage are implemented.

#### CCS minor-subject and specialization rule (APPROVED, CCS-specific)

For the College of Computer Studies:

- Minor subjects are applicable to 1st- and 2nd-year students only.
- Third-year students choose the major or specialization they intend to pursue.
- The curriculum and subject offerings determine which subjects are required for
  each year level and selected major; the solver must not add required subjects
  by rule.
- Minor subjects must not be automatically added to every 1st- or 2nd-year
  section merely because of the year level; each section's offering set comes
  from the curriculum and its configured subject offerings.
- This rule is CCS-specific. It is not a universal scheduling rule for other
  programs or departments, and no other department's curriculum behavior is
  assumed here.
- Fourth-year scheduling follows the applicable curriculum and the selected
  major requirements.

**Scope of this rule in the contract:** it constrains which offerings Laravel
includes in a generation snapshot (the *input-selection* rule). The solver itself
receives the resulting offerings and applies no curriculum logic, no
auto-enrichment of sections, and no year-level-based subject inference.

**UNKNOWN / gap:** the current Laravel data model has no curriculum records, no
major/specialization entity, and no student specialization selection (year
levels are plain 1..6 levels and sections reference only program/year/term).
Which system or table is the source of truth for CCS curriculum requirements and
specialization choice is an open institutional decision (section 12). Until that
source exists, the CCS rule can only be honored by the people who create
offerings; it cannot be enforced or validated by code, and this contract does not
claim it can.

### 4.5 Meeting requirements

Each `meeting_requirements[]` item:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id` | string | yes | Stable unique requirement ID from an approved Laravel meeting plan item. |
| `meeting_type` | string | yes | `lecture`, `laboratory`, `seminar`, or `other`, matching current `class_schedules.schedule_type`. |
| `frequency_per_week` | integer | yes | Positive count of requested occurrences in the weekly timetable. |
| `duration_minutes` | integer | yes | Positive actual duration. No derivation or rounding from `lecture_hours`/`lab_hours`. |
| `modality` | string | yes | `face_to_face`, `online`, or `hybrid`, matching current schedule values. |
| `required_room_type` | string or `null` | yes | Existing room type enum or null. Null means no type-based filter: any available room type is permitted, subject to capacity and active allocations. |
| `required_equipment` | string array | no | Optional sorted unique equipment identifiers. Empty by default when Laravel has no authoritative room-equipment records. A physical candidate room must contain every listed item. |
| `mandatory` | boolean | yes | Mandatory instances are hard constraints; optional instances are desired occurrences and may be omitted only as a soft objective. |
| `allowed_days` | integer array or `null` | yes | Null inherits all term days; otherwise a nonempty subset of term days. |
| `valid_start_times` | `HH:mm` array or `null` | yes | Null inherits global starts; otherwise a nonempty subset of global starts for each allowed day. |

For a mandatory requirement, the solver must schedule exactly
`frequency_per_week` instances. An optional requirement defines a desired maximum
frequency; each omitted instance incurs the configured optional-meeting penalty.
The solver may not drop mandatory meetings to improve its objective.

`online` requirements must have `required_room_type: null` and return `room_id:
null`. `face_to_face` and `hybrid` requirements must choose exactly one physical
room. With no approved in-person enrollment split, both use the full
`expected_enrollment` for capacity checking.

### 4.6 Professors, availability, assignments, and workday policy

`professors[]` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id` | string | yes | Professor ID. |
| `employment_type` | string | yes | `full_time`, `part_time`, or `visiting`. |
| `profile_status`, `account_status` | string | yes | Both must be `active`; this mirrors Laravel's publish check. |
| `availability_windows` | array | yes | Windows for this request's academic term. |
| `part_time_weekly_target_minutes` | integer or `null` | yes | Required and nonnegative for part-time; must be null for full-time and visiting. |

Each availability window is `{ "day_of_week": 1, "start_time": "08:00",
"end_time": "12:00", "availability_type": "available" }`. `availability_type`
must be `available`, `unavailable`, or `preferred`; intervals are same-day,
half-open, and have positive duration. Overlapping windows are invalid, consistent
with the current Laravel availability controller.

Contract availability semantics preserve current Laravel hard validation:

1. Any overlap with an `unavailable` window forbids the meeting.
2. If the professor has one or more `available` or `preferred` windows on that
   day, the complete meeting must fit in one such window. Windows are not merged.
3. If there are no positive (`available` or `preferred`) windows that day, the
   professor is not restricted by a positive availability allow-list; unavailable
   windows still block.
4. `preferred` remains distinct in the request and may receive a soft objective
   preference in the solver. It does not override the hard allow-list rule above.

This preserves existing CCSync publish behavior while allowing the solver to
optimize preference separately. Any change to that existing hard interpretation
requires explicit approval and a contract version/behavior change.

`professor_assignments[]` fields:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `id` | string | yes | Existing `faculty_assignments.id`. |
| `professor_id`, `subject_offering_id` | strings | yes | Must reference included entities. |
| `assignment_role` | string | yes | `lead`, `co_instructor`, `lab_instructor`, or `substitute`. Descriptive; not a primary-selection rule. |
| `status` | string | yes | Must be `active` in a solve request. |
| `eligible_for_scheduling` | boolean | yes | Laravel-derived approval after checking active assignment, active professor and user, active subject/section/program, and open offering. Must be true for rows supplied as candidates. Contains no department, program, or year-level matching (approved rule, "One professor per ordinary offering" in this section). |
| `assigned_units` | decimal string or `null` | yes | Academic units only; never converted into teaching hours. |

#### One professor per ordinary offering (APPROVED, Milestone 4.3)

In scheduling version 1, each ordinary subject offering (subject + section +
academic term) has exactly one distinct Dean-approved professor. The AI
determines only the meeting time and room for that already assigned professor.

- A professor may teach multiple subjects across multiple year levels,
  programs, and departments; one professor holding approved assignments to many
  offerings is normal input, and the solver schedules each offering's time and
  room independently subject to availability, non-overlap, and workload
  constraints.
- Different sections taking the same subject may have different assigned
  professors. Offerings of the same subject in different sections are never
  merged or normalized to a shared instructor.
- Professors from other departments may teach minor subjects or other offerings
  when assigned by authorized academic personnel; eligibility never comes from
  department, program, or year level.
- The AI must never choose between multiple professors for the same ordinary
  offering and must never redistribute professors between sections. If multiple
  eligible distinct professors exist for one ordinary offering, Laravel must
  reject the snapshot as ambiguous before sending; Python never resolves that
  ambiguity.
- Multiple active assignment records for the same professor on one offering
  (for example different roles) are not the same thing as multiple distinct
  professors. The existing ambiguity checks are preserved: ambiguous duplicate
  eligible rows for one professor/offering are still rejected, and assignment
  IDs remain validated.
- One professor per ordinary meeting remains approved (Milestone 4.2).
  Co-instructor support remains deferred.

Consequently, for an ordinary offering the candidate set supplied to Python for
that offering is exactly one professor. Python's only professor-related decisions
are time and room; it cannot transfer, redistribute, or assign professors to
offerings based on availability, department, or year level.

#### Cross-year-level and cross-department assignments (APPROVED)

Eligibility is determined solely by the Dean-approved assignment and the active
status checks above — never by department matching:

- A professor may teach different subjects across multiple year levels, and one
  professor's snapshot may contain assignments to offerings of many sections,
  year levels, and programs. Multiple active assignments for the same professor
  are normal input, and the solver may schedule all of them subject to
  availability, non-overlap, and workload constraints.
- A professor may teach subjects belonging to another department, and minor
  subjects may be handled by professors from other departments. A professor's
  home department must not automatically restrict which subjects they can teach,
  and the subject's owning department must not automatically determine
  professor eligibility.
- The Dean or authorized academic personnel determine which professor is
  assigned to each subject offering. The solver must never transfer,
  redistribute, or assign professors to offerings based only on availability,
  department, or year level; it determines time and room for the already
  assigned professor. In version 1 each ordinary offering has exactly one
  distinct approved professor (section 4.6, "One professor per ordinary
  offering").
- Different sections taking the same subject may have different professors when
  the Dean assigns them that way. Offerings of the same subject in different
  sections are never merged or normalized to a shared instructor.
- Whether the institutional "Dean" maps to an existing Laravel role
  (`administrator` or `department_personnel`, which currently create faculty
  assignments) is an open mapping question (section 12). No new approval role is
  introduced by this contract.

`full_time_workday_policy` is a term snapshot:

```json
{
  "policy_id": "policy-2026-1",
  "policy_version": 1,
  "academic_term_id": "42",
  "standard_days": [1, 2, 3],
  "exceptions": [
    { "professor_id": "501", "authorized_additional_days": [4] }
  ]
}
```

The policy is required when the input contains any full-time professor.
`standard_days` is the term's approved policy; the currently approved initial
standard is Monday-Wednesday. Exceptions are additional authorized days unioned
with the standard days, not removal of a standard day. Python enforces the
resulting allowed days as a hard constraint. Policy ID/version and the resolved
days are part of the immutable snapshot; changing policy must not silently
rewrite a historical or approved generation. Laravel stores policy records and
authorized exception rows. Whether exceptions can also remove standard days is an
institutional decision to confirm.

#### Instructor assignment granularity

**VERIFIED Laravel:** `class_schedules` has many `schedule_assignments`; each
links one `faculty_assignment_id` and has its own `is_primary` and status. Laravel
permits multiple active instructors on one schedule and requires exactly one
primary instructor at publication. `faculty_assignments` are offering-level rows
with roles `lead`, `co_instructor`, `lab_instructor`, or `substitute`. The current
Laravel publish path checks that assignments are active and belong to the offering;
it does not require a particular role for a lecture versus a laboratory meeting.

**VERIFIED Python:** the contract v1.0 adapter returns one `professor_id` per
meeting — the offering's pre-assigned Dean-approved professor, with that
professor's approved assignment reference(s) and `is_primary: true`. Version 1
never chooses between multiple professors for an ordinary offering (section 4.6).
For a combined meeting, the one professor must have an eligible assignment for
each participating offering. Assignment roles are preserved as descriptive source
data; version 1 does not infer a role requirement from `meeting_type`.

This version 1 shape cannot represent a co-taught meeting with two or more
different professors, multiple primary/non-primary instructor rows, or an
explicit requirement such as "one lead plus one lab instructor." Laravel
currently supports multiple schedule assignments, so this is a real narrowing of
existing capability, not a claim that Laravel is single-instructor-only.

**APPROVED (Milestone 4.2):** the institution has approved one professor per
ordinary scheduled meeting for version 1. Co-instructors teaching the same
section and meeting are not part of the current institutional scheduling
requirements, and no multi-instructor meeting logic is implemented for version 1.
The previously stated "approval required" condition is satisfied; version 1 may
be implemented with this limitation. This approval does not change the distinct
rule that different offerings of the same subject may have different assigned
professors, and it does not authorize the solver to assign the same professor to
two overlapping meetings or to move assignments between offerings.

**DEFERRED — possible later extension:** return an `instructors[]` array per
meeting with `professor_id`, exact `faculty_assignment_id`, `assignment_role`,
and `is_primary`; require exactly one primary and one or more eligible
assignments. An approved meeting plan would then state the required instructor
count/roles per meeting. The solver would reserve each selected professor's time
and count each instructor's actual teaching minutes; Laravel would persist one
schedule-assignment row per selected instructor. The role-to-meeting rules,
required counts, workload crediting for co-instruction, and exact
primary-selection rule are not approved or implemented. No role mapping is
invented here.

### 4.7 Rooms

`rooms[]` fields: `id` string, `capacity` positive integer, `room_type` string,
`status` string, and optional sorted unique `equipment` string array. Equipment
is empty by default unless Laravel supplies authoritative room-equipment records.
Only `status: "available"` rooms are sent as candidates.
`room_type` uses the exact current enum:
`lecture|laboratory|seminar|office|other`.

The solver must enforce `capacity >= expected_enrollment` and, when a meeting
specifies `required_room_type`, exact type equality. No unapproved compatibility
mapping (for example, laboratory-to-lecture) is implied. When type is null, no
type comparison is imposed; capacity, availability, and existing allocations
still apply. If a meeting lists `required_equipment`, the room's `equipment`
array must contain every listed identifier; no substitution is inferred.
Permitting an `office` room when the requirement is null is the literal v1.0
rule and requires institutional confirmation.

### 4.8 Explicit combined classes

`combined_classes` may be empty. A nonempty item has:

```json
{
  "id": "combined-1",
  "offering_ids": ["7001", "7002"],
  "shared_meeting_requirements": [
    {
      "id": "shared-lecture-1",
      "source_requirements": [
        { "offering_id": "7001", "meeting_requirement_id": "req-1" },
        { "offering_id": "7002", "meeting_requirement_id": "req-2" }
      ]
    }
  ]
}
```

The group and each shared requirement ID are unique. Each group has at least two
distinct offerings from the same term and distinct sections. Every source
requirement must exist on its referenced offering; each source requirement may be
consumed by only one shared requirement. The participating source requirements
must agree on meeting type, frequency, duration, modality, required room type,
required equipment, mandatory flag, allowed days, and start boundaries. This restricted version 1
shape follows the current Python prototype, which also requires same-subject
offerings, equal frequency, and equal room type. The group is explicit; equality
of subject, course code, or title never creates one automatically. Explicit
cross-subject combined teaching is deferred pending institutional policy.

One shared occurrence satisfies one occurrence of each source requirement.
The room capacity is checked against the sum of participating expected
enrollments. The assigned professor must have an eligible assignment for every
offering; because each ordinary offering has exactly one distinct assigned
professor (section 4.6), a combined meeting requires every participating
offering to have the same assigned professor, and Laravel must reject the group
before sending when the assignments disagree. Workload counts the shared
meeting's actual duration once for the assigned professor, not once per section.

### 4.9 Generation and replacement scope

`generation_scope` is a Laravel-owned, explicit classification of the term's
complete generation target and any separately protected schedule commitments:

```json
{
  "academic_term_id": "42",
  "included_offering_ids": ["7001"],
  "reoptimized_published_schedule_ids": ["8001"],
  "preserved_draft_schedule_ids": ["8002"],
  "replacement_draft_schedule_ids": [],
  "fixed_schedule_ids": []
}
```

| Field | Type | Required | Rule |
|---|---|---:|---|
| `academic_term_id` | string | yes | Must match request metadata and the academic term. |
| `included_offering_ids` | string array | yes | Unique IDs; must exactly equal the IDs in `offerings[]`. These are the offerings whose approved requirements are generated. |
| `reoptimized_published_schedule_ids` | string array | yes | All current published schedule IDs in the term's baseline, except any schedule protected by a separately approved future workflow. In this approved version 1 rule, all published schedules are re-optimized. |
| `preserved_draft_schedule_ids` | string array | yes | Drafts Laravel leaves untouched by this generation. They are not solver constraints by default. |
| `replacement_draft_schedule_ids` | string array | yes | Drafts explicitly selected for replacement; IDs must still be draft, in this term, and belong to an included offering. No draft is implicitly selected. |
| `fixed_schedule_ids` | string array | yes | Only schedules explicitly protected by a separate approved workflow. It is empty in the initial version 1 implementation; published schedules and preserved drafts are not automatically included. |

**APPROVED full-semester re-optimization rule (Milestone 4.4):**

- The complete required offering set is generated regardless of whether an
  offering already has published schedules. Existing published schedule IDs and
  their current assignments/allocations are supplied separately as
  `published_timetable_baseline`; they are context for snapshot validation,
  comparison, and audit, not hard solver constraints. Python solves every
  included offering's approved meeting requirements as a complete candidate;
  it does not calculate residual demand from existing schedule rows. The baseline
  does not limit candidate days, times, rooms, or arrangements.
- Every required offering must be represented in the candidate, and every
  mandatory meeting occurrence must be scheduled. A result that does not meet
  these conditions is infeasible or incomplete and must not be labeled or
  presented as a complete candidate. Explicitly optional occurrences retain
  their existing soft-objective semantics, but their omission must be reported.
- Professor assignments remain fixed to the single Dean-approved professor for
  each ordinary offering (section 4.6). Re-optimization changes schedule
  placement, never instructor selection or distribution.
- Existing drafts are classified by Laravel. Drafts outside an explicit
  replacement selection remain unchanged; they are not automatically fixed
  constraints. Selected draft replacement is applied only as part of successful
  publication, not while the candidate is generated or reviewed. The institution
  must still confirm how draft replacement is selected and handled.
- `fixed_schedule_ids` and `existing_commitments` are reserved for commitments
  explicitly protected through a separate approved workflow. No such protection
  is inferred from published or draft status, and this contract creates no new
  protected-commitment rule. Version 1 has no such workflow and submits an empty
  fixed list.
- Laravel derives all classifications from current database state. A published
  schedule whose offering is not in the authoritative required offering set
  blocks generation for diagnosis; it must not be silently omitted or carried
  into a supposedly complete candidate.

The generation scope and all lists are included in the snapshot hash. Subject
equality, section labels, or time overlap never select a schedule for replacement.

#### Semester-based generation lifecycle (APPROVED)

- The normal workflow generates one timetable covering the entire semester (one
  Laravel academic term), coordinating all configured programs, year levels,
  sections, subject offerings, professors, and rooms. In scope terms, a
  complete-semester generation includes the term's full required offering set;
  required offerings are never silently omitted. See "Complete-semester
  offering coverage" below.
- The generated timetable recurs weekly throughout the applicable semester.
  Weekly recurring schedule blocks are the existing Laravel shape
  (`class_schedules` stores weekday/time blocks, not calendar dates), so no
  per-day records are produced and none are needed for recurrence.
- A new semester requires a new semester timetable: every generation is bound to
  exactly one `academic_term_id`, and cross-term generation is impossible in
  this contract.
- Previous semester schedules remain preserved as historical records. They are
  never deletion targets of a later generation, and cross-term identifiers never
  collide because term scoping is part of every schedule. Candidate and revision
  history are persisted by Laravel; authorized replacement publication remains
  deferred.
- Daily schedule generation is not the normal workflow. Re-running generation
  for a term whose timetable already exists is an explicit full-semester
  re-optimization. Its current published timetable remains active until a
  candidate is approved and atomically published; published placements are not
  fixed constraints for the new candidate.
- A manual mid-semester change remains active until another generation is
  explicitly requested. A later full-semester candidate may move that schedule
  like any other published placement unless it is covered by a separately
  approved protected-commitment workflow.

#### Candidate, approval, publication, and history (APPROVED)

- A solver result is a candidate only. Laravel independently validates it and
  stores it separately from the active timetable; generating or rejecting a
  candidate does not change the active published timetable.
- Candidate approval is a separate Laravel action performed by an authorized
  institutional role. The exact Dean/admin role mapping is unresolved and is not
  assigned by this contract. A solver result, feasible status, or candidate
  persistence is not approval.
- Immediately before publication, Laravel revalidates the candidate and checks
  that its snapshot still matches the current required offerings, approved
  policies, assignments, and active published timetable. A stale snapshot or
  concurrent publication/change is rejected, not silently overwritten.
- Publication replaces the active timetable in one controlled Laravel
  transaction. The new approved candidate becomes active while the previous
  published version is retained as historical data. If any part of publication
  fails, the transaction must leave the previous timetable active and unchanged.
- Rejection leaves the active timetable and historical versions unchanged.
  Published schedules are never physically deleted as part of replacement.
- Term-level candidate/version records and revision history are implemented.
  Authorized approval mapping and atomic publication remain deferred, so these
  bullets describe the required publication contract rather than shipped behavior.

#### Complete-semester offering coverage (APPROVED, Milestone 4.3)

A successful whole-semester generation cannot silently omit required subject
offerings. Laravel, not Python, establishes the authoritative set of required
offerings for the selected academic term.

**Authoritative source of the required offering set:** the required semester
offering set is all `subject_offerings` for the selected term whose status is
`open` and whose section, subject, and program are `active`. Every such offering is
required in the snapshot. Laravel also stores active curriculum requirement rows
and reports offering mismatches in readiness diagnostics, but the authoritative
student specialization source remains unresolved. Coverage validation verifies
internal completeness of what exists; it cannot compensate for offerings that
academic personnel did not create. The CCS minor-subject and specialization rule
therefore remains an offering-creation discipline that readiness diagnostics can
flag but not automatically repair.

**Pre-send validation (IMPLEMENTED Laravel behavior):** before calling Python,
Laravel verifies for every required offering:

1. It is included in the snapshot (`included_offering_ids` exactly matches the
   required set).
2. It has an approved and complete meeting plan (meeting requirements,
   frequencies, durations, modality, room rules — section 4.4).
3. It has its approved professor assignment: exactly one distinct eligible
   professor for an ordinary offering, or the same distinct professor across all
   participants for an explicitly combined group (section 4.6).
4. `expected_enrollment` is non-null wherever a physical or combined meeting
   requires it.

An offering that fails any check makes the whole generation attempt fail. Laravel
returns clear diagnostics identifying each offending offering and why: missing
meeting plan, missing or ambiguous professor assignment, or inconsistent offering
coverage. Incomplete or ambiguous offerings are never silently excluded, and a
partial snapshot must never be sent while being presented as a complete semester
timetable.

#### Manual mid-semester schedule changes (APPROVED)

- Authorized personnel (existing Laravel roles and policies only; no new roles
  or permissions) may manually modify affected schedules during an active
  semester using the existing draft/publish/cancel workflow.
- A manual change must not automatically trigger regeneration of the entire
  semester timetable. No automatic re-solve hook exists in this contract, and
  none may be added implicitly. Regeneration happens only when an authorized
  user explicitly starts a new generation.
- Unaffected schedules are preserved. Manual edits are scoped to the schedules
  a user explicitly modifies; nothing else in the term is touched.
- Conflict and validation handling uses existing Laravel capabilities only:
  publication-time overlap checks against published schedules, persisted
  `schedule_conflicts` records (section, professor, room), conflict
  resolve/waive with mandatory notes, and the draft-only edit rule (published
  schedules are immutable; cancel and replace).
- **Snapshot freshness (PARTIALLY IMPLEMENTED):** if any schedule in the term
  changed after a snapshot was taken, that snapshot is stale. Laravel readiness
  reports published baselines that cannot be linked to a generation; a new
  generation must be built from a fresh snapshot with an incremented
  `snapshot_version`. Transactional rejection of stale candidates at publication
  remains deferred. Python solves the snapshot it receives; it never reconciles a
  stale snapshot with current Laravel state.
- **UNKNOWN:** who performs which mid-semester change (draft creation, room
  reassignment, conflict resolution) under the existing role matrix is already
  VERIFIED Laravel behavior, but the institution has not confirmed whether the
  intended "authorized personnel" map exactly to `administrator` and
  `department_personnel` for every change type.

### 4.10 Existing commitments and active room allocations

`published_timetable_baseline[]` contains every currently published schedule in
the term and is included in the immutable snapshot. Each item records its
`schedule_id`, `subject_offering_id`, optional nullable `meeting_instance_id`,
`day_of_week`, `start_time`, `end_time`, `section_ids`, active `professor_ids`,
and active `room_allocations` (`allocation_id` and `room_id`). A non-null
`meeting_instance_id` is the stable link to one generated meeting occurrence;
legacy schedules without that link remain in the baseline but cannot be used for
placement comparison. The baseline must exactly match
`generation_scope.reoptimized_published_schedule_ids` plus any published schedules
explicitly protected by a separately approved workflow. It is read-only context;
Python must not treat it as occupied solver time.

`existing_commitments[]` would contain exactly one record for every
`fixed_schedule_id`; these are only separately approved fixed constraints, not
the published timetable baseline. Laravel v1.0 sends empty fixed-commitment
arrays because the protected-commitment workflow is unsupported:

| Field | Type | Required | Rule |
|---|---|---:|---|
| `schedule_id` | string | yes | Must belong to `fixed_schedule_ids`. |
| `subject_offering_id` | string | yes | Existing schedule's offering; traceability only. |
| `schedule_status` | string | yes | `published` or `draft`, consistent with Laravel's current state. |
| `day_of_week` | integer | yes | Weekday `1..5`. |
| `start_time`, `end_time` | `HH:mm` strings | yes | Valid weekly half-open interval. |
| `section_ids` | string array | yes | Section resource(s) blocked; include the fixed schedule's section. |
| `professor_ids` | string array | yes | Professors with active schedule assignments and active faculty assignments; empty if none. |
| `room_allocations` | object array | yes | Every active room allocation on the fixed schedule, each `{ "allocation_id": "...", "room_id": "..." }`. |

Only these explicitly protected commitments and exclusions constrain candidate
generation. The current published timetable's schedules and room allocations are
not fixed constraints: they remain active during review, then are superseded
atomically if the candidate is published. Allocation conflicts are checked among
candidate assignments and any explicit fixed commitments, not against the
published baseline that publication will replace.

Existing drafts are not the published baseline. Laravel leaves preserved drafts
untouched. Drafts explicitly selected for replacement are retained during
candidate generation and review; on successful publication, only those selected
drafts may be transitioned to cancelled, with assignment/allocation history
preserved. Rejection or failed publication leaves them unchanged. Exact draft
selection and replacement authorization remain to be confirmed.

Laravel persists a candidate result at most once, using the generation ID plus
meeting-instance ID as its idempotency key. Candidate persistence, revision
history, schedule-to-generation linkage, and response audit records are
implemented. Active timetable replacement and publication history remain deferred.

### 4.11 Solver configuration

`solver_config` is required and separate from business rules. The
`published_placement_change_per_meeting` weight configures only the soft penalty
for changing a deterministically matched published placement. It must be
explicitly supplied and positive; there is no default or institutionally approved
weight. A sample value in this document is provisional and illustrative only.
Hard constraints always take priority, and professor assignments are not a solver
choice.

```json
{
  "time_limit_ms": 30000,
  "worker_count": 1,
  "random_seed": 1,
  "objective_weights": {
    "part_time_target_deviation_per_minute": 10,
    "full_time_load_spread_per_minute": 2,
    "professor_gap_per_minute": 3,
    "section_gap_per_minute": 3,
    "room_capacity_waste_per_seat": 1,
    "section_daily_load_spread_per_minute": 2,
    "preferred_window_miss_per_meeting": 5,
    "optional_meeting_omission": 100,
    "published_placement_change_per_meeting": 1
  }
}
```

`time_limit_ms` and `worker_count` are positive integers. `random_seed` is a
nonnegative integer; multiple workers can make output nondeterministic even with
a seed. Existing objective weights are required nonnegative integers; the
published-placement change weight is a required positive integer. They tune soft
objectives only and cannot relax hard rules. The sample placement weight `1` is
**provisional and illustrative**, not an institutional default or approved
relative priority. Define and version objective semantics before production use;
CP-SAT integer objective scaling must not overflow.

For each baseline meeting with a stable `meeting_instance_id`, prefer retaining
its previous weekday, start time, and room when that exact placement remains
valid. A difference in any of those three fields incurs the configured soft
penalty. The previous professor is not part of this comparison: assignment is
fixed by the approved offering record and cannot be changed by the solver. A
previous placement that violates a current hard constraint is moved; the
continuity penalty cannot make it eligible or override a hard rule.

## 5. Response contract

A successful HTTP response (including a solver-proven infeasible outcome) returns
the following object. `MODEL_INVALID` and service failures use the error envelope
in section 7 rather than a success-shaped response.

```json
{
  "contract_version": "1.0",
  "generation_id": "a11ce000-1111-4111-8111-111111111111",
  "academic_term_id": "42",
  "snapshot_sha256": "64-lowercase-hex-characters",
  "correlation_id": "b22ce000-2222-4222-8222-222222222222",
  "solver_status": "INFEASIBLE",
  "result_status": "INFEASIBLE",
  "started_at": "2026-01-15T10:00:01Z",
  "completed_at": "2026-01-15T10:00:02Z",
  "solver_execution_ms": 950,
  "objective_value": null,
  "best_bound": null,
  "optimality_proven": false,
  "time_limit_reached": false,
  "assignments": [],
  "validation_summary": {
    "independent_validation_status": "not_run",
    "scheduled_meeting_count": 0,
    "required_mandatory_meeting_count": 1,
    "desired_meeting_count": 1,
    "required_offering_count": 1,
    "represented_offering_count": 0,
    "complete_candidate": false,
    "validation_errors": []
  },
  "full_time_distribution": null,
  "placement_metrics": {
    "comparable_meeting_count": 0,
    "retained_placement_count": 0,
    "changed_placement_count": 0,
    "unmatched_baseline_schedule_count": 0
  },
  "unscheduled_mandatory_meeting_ids": null,
  "diagnostics": [
    {
      "code": "MODEL_INFEASIBLE",
      "message": "CP-SAT proved that no assignment satisfies all submitted hard constraints.",
      "evidence": "solver_proven",
      "meeting_requirement_ids": [],
      "related_entity_ids": []
    }
  ],
  "workload_metrics": [],
  "warnings": []
}
```

### 5.1 Generation and solver metadata

| Field | Type | Rule |
|---|---|---|
| `contract_version` | string | Must match the supported response contract. |
| `generation_id` | UUID | Must exactly match the request. |
| `academic_term_id` | string | Must exactly match `academic_term.id` in the request. |
| `snapshot_sha256` | lowercase 64-character hex string | Must exactly match the request hash. |
| `correlation_id` | UUID or `null` | Echo the request value, or null when omitted. |
| `solver_status` | enum | `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, or `UNKNOWN`. |
| `result_status` | enum | `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`, or `TIME_LIMIT`; derived from `solver_status` and `time_limit_reached`. |
| `started_at`, `completed_at` | RFC 3339 UTC | Solver execution timestamps; completion is not before start. |
| `solver_execution_ms` | nonnegative integer | Solver wall time in milliseconds. |
| `objective_value`, `best_bound` | finite number or `null` | Null when the solver has no applicable value/bound. |
| `optimality_proven` | boolean | True only for `OPTIMAL`. |
| `time_limit_reached` | boolean | True if the configured solver deadline ended search. |
| `full_time_distribution` | object or `null` | Soft full-time workload spread metric, or null when unavailable. |
| `placement_metrics` | object | Comparable, retained, changed, and unmatched published-placement counts; see section 5.3. |

`OPTIMAL` means CP-SAT proved optimality; `FEASIBLE` means it found a valid
solution but did not prove optimality; `INFEASIBLE` means it proved no assignment
satisfies the hard constraints; `UNKNOWN` means it proved neither feasibility nor
infeasibility (for example, a deadline before finding a solution). A `FEASIBLE`
response may have omitted optional occurrences but must contain every mandatory
occurrence. `complete_candidate` is true only when every required offering is
represented by at least one scheduled meeting, all mandatory meeting occurrences
are present, and independent validation passed. An incomplete result can never be
presented or persisted as a complete candidate. A timeout does not itself mean
infeasibility.

### 5.2 Assignments

Each `assignments[]` item:

| Field | Type | Rule |
|---|---|---|
| `meeting_instance_id` | string | Unique occurrence ID, deterministically derived from source requirement ID and 1-based occurrence number (or the combined requirement ID and occurrence). |
| `source_requirement_id` | string | Requirement or shared requirement that generated this occurrence. |
| `occurrence_number` | positive integer | One-based occurrence within that requirement. |
| `subject_offering_ids`, `section_ids` | string arrays | One offering/section normally; all participants for an explicit combined meeting. |
| `professor_id` | string | The offering's pre-assigned eligible professor. In version 1 this is the Dean-approved professor of the ordinary offering (section 4.6), not a solver choice between candidates. |
| `professor_assignments` | object array | One entry for an ordinary meeting; for a combined meeting, one entry per participating offering. Each entry has `subject_offering_id`, `faculty_assignment_id`, and `assignment_role`, all copied from the exact eligible input row for `professor_id`. |
| `is_primary` | boolean | Always `true` for the one selected instructor in version 1. Laravel still enforces exactly one primary schedule assignment. |
| `room_id` | string or `null` | Null only for online; otherwise one eligible physical room. |
| `meeting_type`, `modality` | strings | Exact values from the source meeting requirement. |
| `day_of_week` | integer | Weekday `1..5`. |
| `start_time`, `end_time` | `HH:mm` strings | Local wall-clock time; half-open interval. End is computed from duration. |
| `duration_minutes` | positive integer | Must equal the source requirement; no period indexes as sole time representation. |

The result includes real times and source requirement IDs, not a bare `period`
index. Each assignment reference keeps the offering relationship explicit. For an
ordinary meeting the returned professor is exactly the offering's pre-assigned
distinct professor; version 1 results contain no solver-made professor selection,
transfer, or redistribution. Laravel's
current schedule schema links one schedule to one offering, so persistence of one
shared meeting across multiple offerings requires future representation.

### 5.3 Validation summary, diagnostics, workload, and warnings

`validation_summary` fields:

- `independent_validation_status`: `passed`, `failed`, or `not_run`.
- `scheduled_meeting_count`: number of returned assignment instances.
- `required_mandatory_meeting_count`: expected count of mandatory instances.
- `desired_meeting_count`: expected count of mandatory plus optional instances.
- `required_offering_count`: count of offerings in the authoritative snapshot.
- `represented_offering_count`: count of required offerings with at least one
  returned meeting assignment.
- `complete_candidate`: true only when every required offering is represented,
  every mandatory occurrence appears exactly once, and independent validation
  passed.
- `validation_errors`: array of machine-readable validation messages; must be empty
  when status is `passed`.

`full_time_distribution` and `placement_metrics` are top-level response fields,
not members of `validation_summary`. `full_time_distribution` is an object or
null; when present it contains `minimum_assigned_minutes`,
`maximum_assigned_minutes`, and `spread_minutes`. `placement_metrics` is a
required object containing nonnegative `comparable_meeting_count`,
`retained_placement_count`, `changed_placement_count`, and
`unmatched_baseline_schedule_count`.

For `OPTIMAL` or `FEASIBLE`, independent validation must be `passed` and
`complete_candidate` must be true before assignments are usable as a full-semester
candidate. An offering with only optional requirements must still have at least
one scheduled meeting to count as represented. For `INFEASIBLE` or `UNKNOWN`, set
the status to `not_run` if there is no candidate assignment, set
`complete_candidate` to false, and do not label an empty result valid.

`placement_metrics` reports `comparable_meeting_count`,
`retained_placement_count`, `changed_placement_count`, and
`unmatched_baseline_schedule_count`. Comparison uses only published baseline
rows with a non-null stable `meeting_instance_id` and enough unambiguous placement
data, keyed by that instance ID. A placement is retained only when weekday,
start time, and room all match; otherwise it is changed, including when a
previously published optional meeting is omitted. Baseline rows without reliable
meeting-instance linkage are counted as unmatched and excluded from retention
and change counts. When no baseline placement is comparable, the first three
counts are zero. These are comparison metrics, not solver status or hard
constraints.

The change objective and placement metrics do not select, move, or redistribute
professors. The response is rejected if its professor assignment differs from
the single pre-assigned professor in the request.

`unscheduled_mandatory_meeting_ids` is `null` unless the solver has specific
occurrence IDs from a supported diagnostic procedure. When a mandatory meeting
has zero deterministic placement candidates during input checking, that exact
`meeting_instance_id` may be listed. With mandatory meetings modeled as hard
constraints, CP-SAT generally does not identify a subset that caused global
infeasibility. Do not fabricate one.

Each `diagnostics[]` entry has `code`, `message`, `evidence`,
`meeting_requirement_ids`, and `related_entity_ids`. `evidence` is one of:

- `solver_proven`: only a solver-proven conclusion, such as global infeasibility.
- `input_check`: a deterministic pre-solve finding, such as no eligible professor
  for a requirement or no capacity/type/equipment-compatible room. The message
  includes stable reason codes for the failed input checks.
- `heuristic`: a possible explanation that is not proven by CP-SAT.

An input check or heuristic is not an unsatisfiable core. Do not claim a precise
cause for global infeasibility unless the solver actually established it.

Each `workload_metrics[]` entry has `professor_id`, `employment_type`,
`assigned_teaching_minutes`, `weekly_target_minutes` (integer or null),
`signed_target_deviation_minutes` (`assigned - target`, or null), and
`absolute_target_deviation_minutes` (or null). Teaching time is the sum of
scheduled meeting durations for that professor, once per shared meeting, not once
per participating section. Targets are soft; hard constraints take precedence.
Full-time professors have no universal hours quota. `full_time_distribution` may
report `minimum_assigned_minutes`, `maximum_assigned_minutes`, and
`spread_minutes`; it is a soft balance metric, not a workload rule.

`warnings[]` entries have `code`, `message`, and optional related IDs. Warnings
may describe part-time target deviation, missed preferred windows, omitted
optional meetings, or a non-optimal feasible result. They must never suggest that
a hard constraint was violated in a result marked independently valid.

## 6. Example requests and responses

The following request illustrates one explicit 90-minute lecture. `Asia/Manila`
and the IDs are illustrative only; the actual institutional timezone and data must
be supplied by Laravel. Hash shown is a schema placeholder and must be computed
from the exact canonical request content as specified in section 3.2.

### 6.1 Feasible request

```json
{
  "contract_version": "1.0",
  "metadata": {
    "generation_id": "a11ce000-1111-4111-8111-111111111111",
    "requested_at": "2026-01-15T10:00:00Z",
    "academic_term_id": "42",
    "snapshot_version": 1,
    "snapshot_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "correlation_id": "b22ce000-2222-4222-8222-222222222222"
  },
  "academic_term": {
    "id": "42",
    "academic_year": "2025-2026",
    "term": "second",
    "name": "Second Term",
    "starts_on": "2026-02-02",
    "ends_on": "2026-05-30",
    "status": "planned",
    "applicable_days": [1, 2, 3]
  },
  "time_grid": {
    "configuration_id": "grid-term-42",
    "version": 1,
    "academic_term_id": "42",
    "approval_status": "approved",
    "approved_at": "2026-01-10T09:00:00Z",
    "timezone": "Asia/Manila",
    "days": [
      {"day_of_week": 1, "operating_start": "08:00", "operating_end": "17:00", "valid_start_times": ["08:00", "09:00", "10:00"]},
      {"day_of_week": 2, "operating_start": "08:00", "operating_end": "17:00", "valid_start_times": ["08:00", "09:00", "10:00"]},
      {"day_of_week": 3, "operating_start": "08:00", "operating_end": "17:00", "valid_start_times": ["08:00", "09:00", "10:00"]}
    ]
  },
  "generation_scope": {
    "academic_term_id": "42",
    "included_offering_ids": ["7001"],
    "reoptimized_published_schedule_ids": [],
    "preserved_draft_schedule_ids": [],
    "replacement_draft_schedule_ids": [],
    "fixed_schedule_ids": []
  },
  "published_timetable_baseline": [],
  "programs": [{"id": "3", "code": "BSIT", "name": "Information Technology", "status": "active"}],
  "year_levels": [{"id": "2", "level": 2, "name": "Second Year"}],
  "sections": [
    {
      "id": "201",
      "program_id": "3",
      "year_level_id": "2",
      "academic_term_id": "42",
      "code": "BSIT-2A",
      "name": "BSIT Second Year A",
      "capacity": 40,
      "status": "active",
      "availability_windows": []
    }
  ],
  "subjects": [
    {"id": "88", "code": "IT201", "title": "Example Subject", "status": "active", "units": "3.0", "lecture_hours": "3.0", "lab_hours": "0.0"}
  ],
  "offerings": [
    {
      "id": "7001",
      "academic_term_id": "42",
      "section_id": "201",
      "subject_id": "88",
      "expected_enrollment": 30,
      "status": "open",
      "meeting_plan_id": "plan-7001",
      "meeting_plan_version": 1,
      "meeting_plan_approval_status": "approved",
      "meeting_plan_approved_at": "2026-01-12T10:00:00Z",
      "meeting_requirements": [
        {
          "id": "req-7001-lecture",
          "meeting_type": "lecture",
          "frequency_per_week": 1,
          "duration_minutes": 90,
          "modality": "face_to_face",
          "required_room_type": null,
          "mandatory": true,
          "allowed_days": null,
          "valid_start_times": null
        }
      ]
    }
  ],
  "professors": [
    {
      "id": "501",
      "employment_type": "full_time",
      "profile_status": "active",
      "account_status": "active",
      "availability_windows": [
        {"day_of_week": 1, "start_time": "08:00", "end_time": "12:00", "availability_type": "available"}
      ],
      "part_time_weekly_target_minutes": null
    }
  ],
  "professor_assignments": [
    {
      "id": "9001",
      "professor_id": "501",
      "subject_offering_id": "7001",
      "assignment_role": "lead",
      "status": "active",
      "eligible_for_scheduling": true,
      "assigned_units": "3.0"
    }
  ],
  "full_time_workday_policy": {
    "policy_id": "policy-2026-1",
    "policy_version": 1,
    "academic_term_id": "42",
    "standard_days": [1, 2, 3],
    "exceptions": []
  },
  "rooms": [
    {"id": "30", "capacity": 40, "room_type": "lecture", "status": "available"}
  ],
  "combined_classes": [],
  "existing_commitments": [],
  "solver_config": {
    "time_limit_ms": 30000,
    "worker_count": 1,
    "random_seed": 1,
    "objective_weights": {
      "part_time_target_deviation_per_minute": 10,
      "full_time_load_spread_per_minute": 2,
      "professor_gap_per_minute": 3,
      "section_gap_per_minute": 3,
      "room_capacity_waste_per_seat": 1,
      "section_daily_load_spread_per_minute": 2,
      "preferred_window_miss_per_meeting": 5,
      "optional_meeting_omission": 100,
      "published_placement_change_per_meeting": 1
    }
  }
}
```

### 6.2 Optimal response

```json
{
  "contract_version": "1.0",
  "generation_id": "a11ce000-1111-4111-8111-111111111111",
  "academic_term_id": "42",
  "snapshot_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "correlation_id": "b22ce000-2222-4222-8222-222222222222",
  "solver_status": "OPTIMAL",
  "result_status": "FEASIBLE",
  "started_at": "2026-01-15T10:00:01Z",
  "completed_at": "2026-01-15T10:00:02Z",
  "solver_execution_ms": 950,
  "objective_value": 0,
  "best_bound": 0,
  "optimality_proven": true,
  "time_limit_reached": false,
  "assignments": [
    {
      "meeting_instance_id": "req-7001-lecture:1",
      "source_requirement_id": "req-7001-lecture",
      "occurrence_number": 1,
      "subject_offering_ids": ["7001"],
      "section_ids": ["201"],
      "professor_id": "501",
      "professor_assignments": [
        {"subject_offering_id": "7001", "faculty_assignment_id": "9001", "assignment_role": "lead"}
      ],
      "is_primary": true,
      "room_id": "30",
      "meeting_type": "lecture",
      "modality": "face_to_face",
      "day_of_week": 1,
      "start_time": "09:00",
      "end_time": "10:30",
      "duration_minutes": 90
    }
  ],
  "validation_summary": {
    "independent_validation_status": "passed",
    "scheduled_meeting_count": 1,
    "required_mandatory_meeting_count": 1,
    "desired_meeting_count": 1,
    "required_offering_count": 1,
    "represented_offering_count": 1,
    "complete_candidate": true,
    "validation_errors": []
  },
  "full_time_distribution": {
    "minimum_assigned_minutes": 90,
    "maximum_assigned_minutes": 90,
    "spread_minutes": 0
  },
  "placement_metrics": {
    "comparable_meeting_count": 0,
    "retained_placement_count": 0,
    "changed_placement_count": 0,
    "unmatched_baseline_schedule_count": 0
  },
  "unscheduled_mandatory_meeting_ids": null,
  "diagnostics": [],
  "workload_metrics": [
    {
      "professor_id": "501",
      "employment_type": "full_time",
      "assigned_teaching_minutes": 90,
      "weekly_target_minutes": null,
      "signed_target_deviation_minutes": null,
      "absolute_target_deviation_minutes": null
    }
  ],
  "warnings": []
}
```

### 6.3 Infeasible response

This example deliberately does not claim which individual constraint caused the
conflict.

```json
{
  "contract_version": "1.0",
  "generation_id": "a11ce000-1111-4111-8111-111111111111",
  "academic_term_id": "42",
  "snapshot_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "correlation_id": "b22ce000-2222-4222-8222-222222222222",
  "solver_status": "INFEASIBLE",
  "result_status": "INFEASIBLE",
  "started_at": "2026-01-15T10:00:01Z",
  "completed_at": "2026-01-15T10:00:02Z",
  "solver_execution_ms": 800,
  "objective_value": null,
  "best_bound": null,
  "optimality_proven": false,
  "time_limit_reached": false,
  "assignments": [],
  "validation_summary": {
    "independent_validation_status": "not_run",
    "scheduled_meeting_count": 0,
    "required_mandatory_meeting_count": 1,
    "desired_meeting_count": 1,
    "required_offering_count": 1,
    "represented_offering_count": 0,
    "complete_candidate": false,
    "validation_errors": []
  },
  "placement_metrics": {
    "comparable_meeting_count": 0,
    "retained_placement_count": 0,
    "changed_placement_count": 0,
    "unmatched_baseline_schedule_count": 0
  },
  "unscheduled_mandatory_meeting_ids": null,
  "diagnostics": [
    {
      "code": "MODEL_INFEASIBLE",
      "message": "CP-SAT proved that no assignment satisfies all submitted hard constraints.",
      "evidence": "solver_proven",
      "meeting_requirement_ids": [],
      "related_entity_ids": []
    }
  ],
  "workload_metrics": [],
  "full_time_distribution": null,
  "warnings": []
}
```

## 7. API and error semantics

### 7.1 Endpoint and synchronous behavior

- Implemented solve endpoint: `POST /api/v1/solve` on the Python service. Laravel
  calls it through `SchedulerClient`; browser clients never call Python directly.
- Implemented validation-only route: `POST /api/v1/validate`. It applies strict
  request-schema and deterministic semantic validation and returns only a valid
  marker plus generation metadata and offering count. It does not invoke CP-SAT,
  create a candidate, or persist data.
- `/api/v1/solve`, `/api/v1/validate`, and `/health/ready` require the
  `X-CCSync-Key` header. `/health/live` is public and is the Render health-check
  target.
- Laravel waits for one synchronous response. This is the current production
  transport, not a final scaling commitment.
- The Python service validates the full request, runs CP-SAT up to
  `time_limit_ms`, independently validates any returned solution, and responds
  once. A successful solver outcome is HTTP `200`, including `INFEASIBLE` and
  `UNKNOWN`.
- Health endpoints are service health checks, not generation status endpoints.
- No poll, result retrieval, or cancellation endpoint is part of v1.0. Closing a
  client connection or timing out does not guarantee that CP-SAT stopped; the
  solver remains bounded by `time_limit_ms`.
- No Celery, Redis, Python background worker, or polling API is part of v1.0.
  Whether generation needs an asynchronous lifecycle is deferred until full-scale
  measurements are available.

### 7.2 Transport limits and client timeouts

- Maximum JSON request body: **10 MiB**, enforced before Pydantic parsing. This
  is an implemented service limit, not a verified campus-scale capacity claim.
- Service-side maximum `time_limit_ms`: **300,000 ms**. A request over this cap
  is rejected before solving. The value is a hard cap, not an approved SLA.
- Laravel `SCHEDULER_CONNECT_TIMEOUT` and `SCHEDULER_TIMEOUT` are explicit
  environment settings. `SCHEDULER_TIMEOUT` must exceed the approved solver time
  limit by a bounded network and serialization margin, and must remain consistent
  with PHP/web-server and proxy limits.
- If an HTTP timeout occurs, Laravel treats the result as a `scheduler_timeout`
  transport failure and must not infer infeasibility. Generation retry policy
  remains a Laravel concern.
- Reproducibility is not guaranteed for multiple CP-SAT workers or a timeout.
  Record request hash, solver version, configuration, seed, and response for audit.

#### Evidence required before choosing a production execution model

Before deciding whether synchronous generation remains appropriate or an
asynchronous lifecycle is necessary, measure representative full-timetable
workloads and record:

1. End-to-end and CP-SAT solve durations (including median, tail, and worst
   observed duration), split by input scale and solver status.
2. Serialized request and response sizes, including the largest expected result.
3. Maximum expected meeting-instance count, plus the associated sections,
   offerings, professors, rooms, and fixed commitments.
4. Expected and peak concurrent generation requests, and whether concurrent
   generations for the same term are permitted.
5. Laravel HTTP client, PHP/web-server, proxy, and any queue-worker/job timeout
   limits; determine whether the user-facing request can wait for the full bound.
6. Solver CPU/memory consumption and the effect of worker count under the
   expected concurrency.

Until that evidence is reviewed, keep the current implementation synchronous.
Do not infer production suitability from the synthetic demo or add background
infrastructure without a separate decision.

### 7.3 Error envelope and HTTP status

```json
{
  "error": {
    "code": "invalid_contract",
    "message": "The request does not satisfy contract version 1.0.",
    "details": [
      {"path": "offerings[0].meeting_requirements[0].duration_minutes", "code": "must_be_positive"}
    ],
    "correlation_id": "b22ce000-2222-4222-8222-222222222222",
    "retryable": false
  }
}
```

Implemented status mapping:

| HTTP | Code | Meaning |
|---:|---|---|
| `400` | `invalid_json`, `invalid_content_length` | Malformed JSON, duplicate keys, or invalid transport headers. |
| `401` | `invalid_api_key` | Missing or invalid `X-CCSync-Key`. |
| `413` | `request_too_large` | Request exceeds the 10 MiB body limit. |
| `415` | `unsupported_media_type` | `Content-Type` is not `application/json`. |
| `422` | `invalid_contract` | Unsupported contract version, schema failure, inconsistent references, or semantic validation failure. |
| `500` | solver-specific error | Internal error, CP-SAT `MODEL_INVALID`, or independent validation failure. |
| `503` | `scheduler_auth_not_configured` | Python has no configured `SCHEDULER_API_KEY`; fail closed. |
| `503` | n/a | Application or OR-Tools readiness check failed; FastAPI returns a detail message. |

Error details must not include stack traces, secrets, or full snapshots. Python
must not return a partial candidate after independent validation fails.

### 7.4 Duplicate IDs and statelessness

The local Python service is stateless and has no durable idempotency store. A
repeated UUID with the same hash is a new solve attempt and may produce a different
feasible result. Python cannot detect reuse of a UUID with a different payload
across requests. Laravel owns uniqueness: it must reject a different hash for an
existing generation ID, retain the first accepted result, and associate retries
with the same immutable snapshot. The service does not promise deduplication.

## 8. Data normalization rules

| Data | Input | Solver representation and rule | Validation/error behavior |
|---|---|---|---|
| Weekday | Laravel may store ISO `day_of_week` integer `1..7` | Contract v1.0 accepts `1=Mon` through `5=Fri` and uses those values directly. | Reject values outside `1..5`; do not infer weekday from localized names. |
| Time | Laravel `H:i` local time and request timezone | Convert exactly to integer minutes after midnight. Use half-open intervals. | Strict `HH:mm`; require start `<` end; reject overnight/cross-day meetings. Never round. |
| Operating hours and valid starts | Explicit term policy and boundaries | Use each day's exact operating interval and valid start list. A meeting ends at start plus exact duration; end need not be a valid start. | Require unique sorted boundaries within operating hours. Reject a result whose start is not an allowed boundary or whose end exceeds close. |
| Faculty availability | Term-scoped windows with `available`, `unavailable`, `preferred` | Preserve each typed window. Apply the hard allow/deny semantics in section 4.6; preferred remains a separate soft objective signal. | Reject invalid or overlapping windows. Windows outside operating time are retained; only candidate intervals intersecting them can be used. Do not clip or round. |
| Lecture/lab duration and frequency | Explicit meeting plan | Use `duration_minutes` and `frequency_per_week` directly; create one instance per occurrence. | Positive integer duration/count; no derivation from catalog `lecture_hours`, `lab_hours`, or units. A nonintegral-minute rule is invalid rather than rounded. |
| Part-time target | Proposed `part_time_weekly_target_minutes` | Integer minutes; objective deviation is `abs(actual_teaching_minutes - target_minutes)`. If a future UI stores decimal hours, convert exactly by `hours * 60` only when the result is an integer minute. | Required for part-time, null for other employment types; no credits or periods conversion. Reject fractional-minute targets. |
| Actual workload | Scheduled meeting intervals | Sum each professor's assigned meeting duration once per weekly occurrence; for an explicitly shared meeting, count it once. | Report actual assigned minutes and signed/absolute target deviation; target remains soft. |
| Room type/capacity/status/equipment | Existing room record | Candidate rooms must be `available`, satisfy `capacity >= total expected enrollment`, exactly match specified `required_room_type`, and contain every `required_equipment` identifier. Null type means no type filter. | Reject nonpositive capacities and unknown statuses/types. If physical enrollment is null, Laravel must not submit a solvable request. No implicit type or equipment compatibility. |
| Professor assignments | Current faculty assignment records | Include only active approved assignments, carrying role and assignment ID. Solver candidate set is the exact set supplied by Laravel; combined meeting uses intersection across offerings. | Reject missing/foreign IDs, inactive candidates, or ambiguous duplicate active rows for one professor/offering until role selection is defined. |
| Combined enrollment | One expected enrollment per unique offering | Sum each participant once for physical room capacity; shared workload remains one duration. | Reject duplicate offerings, null physical enrollment, or nonmatching shared meeting requirements. |
| Term dates/days | Term dates plus explicit approved scheduling days | Preserve ISO inclusive dates and weekly ISO weekdays. Require each declared weekday to occur at least once in term. | No automatic holiday exclusion or day selection from term name. Date-specific calendars remain unsupported. |
| Modality | Existing schedule modality values, attached to proposed meeting plan | `online`: no physical room; `face_to_face`/`hybrid`: one room, capacity uses full enrollment. | Reject online room requirements and physical meetings without a compatible room. Hybrid attendance splits are unsupported. |

## 9. Validation responsibility

### 9.1 Laravel before sending

Laravel must construct and validate a single term-consistent immutable snapshot:

- The term is `active` or `planned`, dates are valid, and each applicable day occurs
  within its inclusive range.
- The institutional time-grid configuration is approved, versioned, associated
  with this term, and is the source of timezone, operating hours, and boundaries.
- Included programs, sections, subjects, professor profiles and accounts are
  active; offerings are open and match the term/section/subject relationships.
- Every offering has explicit meeting requirements with durations, frequencies,
  modality, room rules, and optionality. Its exact meeting-plan version must be
  approved. Catalog hours are not substitutes, and Laravel must not invent an
  approving role that the institution has not selected.
- Every faculty candidate is an active, approved `faculty_assignments` row for the
  exact offering, with active profile and account. A role is not converted into
  primary status.
- Availability is valid for this term and every candidate assignment complies
  with its hard allow/deny semantics. Preferred windows are preserved. The
  published timetable baseline is not an availability or occupancy constraint.
- Physical meetings have an authoritative non-null expected enrollment, a
  compatible available room candidate, and no conflict with separately approved
  fixed commitments. Allocations in the published baseline are recorded but do
  not block candidate room selection.
- The applicable versioned full-time policy and authorized exceptions are resolved
  for this term; no current table stores them.
- Combined groups are explicit and complete; every shared source requirement,
  professor intersection, and enrollment total is validated.
- Every current published schedule and its active allocations are captured in
  `published_timetable_baseline`; those placements are not fixed solver
  commitments. The baseline's offering must belong to the authoritative required
  offering set, or Laravel rejects the generation with diagnostics.
- `generation_scope` includes every required offering, classifies published and
  draft schedules, and lists only separately approved fixed commitments.
  Published schedules are re-optimized by default; preserved drafts are not
  automatically fixed; replacement IDs identify only explicitly selected drafts.
- Candidate creation and rejection do not change the active published timetable.
  Before publication Laravel must revalidate the candidate and snapshot, obtain
  authorized approval, and atomically replace the active timetable while retaining
  the prior version for history.
- Incomplete or ambiguous input is rejected before sending. Laravel must not
  invent meeting plans, workload targets, policies, or combined classes.
- The included offering set reflects the approved curriculum rules, including
  the CCS-specific minor-subject and specialization rule: minor subjects are
  included for 1st/2nd-year sections only when the curriculum and configured
  offerings require them, third/fourth-year sections follow the selected major
  requirements, and the solver applies no curriculum logic of its own. Laravel
  must not auto-add minor subjects by year level or derive the offering set from
  department ownership. (No curriculum data source currently exists in Laravel;
  see section 4.4.)
- Generation is semester-scoped: the snapshot covers the whole term, the
  generated timetable is weekly recurring, and no daily generation schedule is
  assumed. If schedules in the term changed after the snapshot was taken, the
  snapshot is stale and a new generation requires a fresh snapshot with a new
  `snapshot_version` and re-derived scope/commitments.
- Complete-semester coverage is verified before sending (section 4.9): the
  authoritative required offering set is the term's open subject offerings with
  active section/subject/program; every required offering is included, has an
  approved complete meeting plan, and has exactly one distinct approved
  professor assignment (the same distinct professor across participants for a
  combined group). Missing plans, missing or ambiguous professor assignments, or
  inconsistent coverage reject the generation attempt with diagnostics naming
  the offending offerings. Incomplete offerings are never silently excluded.
- A generation UUID, request timestamp, snapshot version/hash, and correlation ID
  are recorded. The hash is verified before persistence of the eventual result.

### 9.2 Python before solving

Python validates with strict Pydantic models and semantic checks:

- Contract version, identifier format, required/nullability rules, enum values,
  hash, request size, timestamps, and cross-references.
- Approved time-grid configuration ID/version/term and approved meeting-plan
  status/version; Python checks declared provenance but does not approve either.
- Exact generation-scope classification and consistency between the published
  timetable baseline, re-optimized published IDs, explicit fixed commitments,
  included offerings, and explicitly selected draft replacement IDs. Published
  baseline rows are validated as input context, never treated as occupied solver
  intervals.
- Term/day/time-grid consistency, valid boundary lists, exact positive durations,
  and intervals that fit operation hours.
- Unique offering/section/subject/professor/room/requirement IDs and correct
  academic-term relationships.
- Professor assignment eligibility and exact offering mapping; exactly one
  distinct eligible professor per ordinary offering (ambiguity must never reach
  the solver; a request with multiple distinct eligible professors for one
  ordinary offering is semantically invalid); combined candidates are eligible
  for every source offering.
- Internal offering-coverage consistency: `generation_scope.included_offering_ids`
  equals `offerings[]`, every included offering has an approved complete meeting
  plan, and every included offering resolves to exactly one distinct assigned
  professor (or the same professor across a combined group). Python cannot detect
  offerings that Laravel omitted from the required set — authoritative coverage is
  a Laravel responsibility (section 4.9).
- A feasible result is complete only if it schedules every mandatory occurrence
  and represents every required offering with at least one meeting assignment.
  An incomplete result must not be returned as a usable full-semester candidate.
- Availability overlap/type rules, term association, and full-time policy/exception
  applicability.
- Room operational status, capacity, room type, modality, and existing commitments.
- Hard constraints against other candidate assignments and explicitly protected
  commitments. The currently published timetable baseline is not a hard
  constraint.
- Combined group membership, source requirements, enrollment, and constraints.
- Positive bounded solver limits, nonnegative weights, and supported settings.

Input validation errors return HTTP 422. A structurally valid but unsatisfiable
problem is a solver outcome, not a request-validation error.

### 9.3 Python after solving

Before returning `OPTIMAL` or `FEASIBLE`, Python independently validates:

- Every mandatory occurrence appears exactly once; no unknown or duplicate meeting
  instance is returned, and every required offering has at least one returned
  meeting assignment.
- All candidate professor, section, and room intervals are nonoverlapping with
  one another and with explicitly protected commitments; touching endpoints are
  allowed. Published baseline intervals are excluded because they may be
  replaced at publication.
- Availability, operating hours, valid starts, term workdays, and applicable
  exceptions are respected.
- Professor and room IDs are among the supplied eligible inputs.
- Room capacity, room type, modality, and combined enrollment are valid.
- Combined meeting satisfies each participant's approved professor assignment and
  source meeting requirement.
- Actual duration/end time and all response references match the input snapshot.

If independent validation fails, do not report a usable feasible result; return an
internal error and preserve details only in protected service logs.

### 9.4 Laravel after receiving

Before persisting a candidate, Laravel must independently verify:

- Response `generation_id` and contract version match the request and its saved
  snapshot hash; response hash and correlation ID match the submitted request.
- Every meeting instance and source requirement exists in the submitted snapshot;
  each mandatory instance is present exactly once, optional instances are handled
  according to the response, and every required offering has at least one returned
  meeting. An incomplete result is never persisted or labeled a complete candidate.
- Every `subject_offering_id`, `section_id`, professor, assignment ID/role, and
  room belongs to the snapshot. Professor assignment is still active and approved,
  and the returned professor for each ordinary offering is still that offering's
  single distinct assigned professor (section 4.6); no additional eligible
  professor was created for the offering between snapshot and response.
- Times are on the agreed boundaries, preserve exact duration, and comply with the
  term's immutable policy snapshot.
- Room is still available and suitable, has sufficient capacity, and has no
  allocation conflict with the candidate or separately protected commitments.
  Exclude allocations in the active published baseline that this publication
  will atomically supersede; re-read all relevant state within the transaction.
- Candidate meetings do not conflict with one another or any separately approved
  fixed commitments. The published baseline is not checked as an occupancy
  blocker because it remains active until the atomic replacement operation.
- The generation scope and snapshot are still current. Any change to required
  offerings, approved policies/assignments, or the active published timetable
  after snapshot creation makes the candidate stale and blocks publication.
- Candidate creation and rejection leave the active published timetable unchanged.
  Existing drafts remain unchanged unless explicitly selected for replacement;
  selected drafts are transitioned only as part of successful publication.
- No candidate is persisted from an `INFEASIBLE`, `UNKNOWN`, incomplete, invalid,
  or failed result.
- Publication uses an authorized Laravel approval operation. Existing Laravel
  hard conflict, primary-instructor, and room rules still apply, evaluated against
  the candidate and separately protected commitments rather than the published
  version being superseded. In one transaction, Laravel revalidates current state,
  retains the previous published version as history, and activates the approved
  candidate. Any failure rolls back the entire replacement and leaves the
  previous timetable active. Repeated publication is idempotent; stale or
  concurrent candidates are rejected.

Laravel persists candidates separately from the active timetable and closes the
snapshot-to-publication race by revalidating current database state in the
publication transaction.

## 10. Security and reliability boundaries

- Python is a private local development service initially. A deployed Laravel
  service cannot reach a developer's `localhost`; the local integration requires
  an explicitly approved secured development connection. Do not expose the solver
  publicly or configure a tunnel as part of this milestone.
- Service-to-service authentication is separate from browser Sanctum SPA
  authentication. Before any non-loopback connection, require a narrowly scoped
  service credential or mutually authenticated transport, TLS, rotation, and
  caller/network restrictions. Credentials must not be committed or logged.
- Do not send database credentials, student names, professor names, emails, tokens,
  or unrelated PII. Use IDs and only the scheduling attributes needed to solve.
- Treat schedule, availability, enrollment, and workload data as sensitive
  institutional data. Restrict access to snapshots, logs, backups, and diagnostics.
- Logs should contain generation/correlation IDs, hash, contract/solver versions,
  status, and timing; do not log full request payloads or credentials by default.
- Validate request size and all values before model construction; cap solver time
  and workers. A timeout or dropped connection is indeterminate, not infeasible.
- Python has no persistence authority. Laravel validates returned results again,
  guards concurrent generation with its own records/locks, and alone persists
  draft schedules.

## 11. Future Laravel persistence and implementation sequence

### Fields/records requiring future Laravel support

These are not claimed to exist:

1. Offering meeting-plan records: stable plan and requirement IDs, term/offering
   association, meeting type, weekly frequency, exact duration minutes, modality,
   mandatory/optional flag, room type, day/start-boundary restrictions, version,
   approval status/time, and audit provenance for the approving actor. The
   institutional author/approver roles remain undecided.
2. Individual part-time professor weekly target in minutes (or an exactly
   convertible decimal-hour value) and effective term.
3. Full-time workday policy versions tied to academic terms and authorized
   professor/day exceptions; policy history must remain immutable for prior
   generations and approved schedules.
4. Term-associated institutional time-grid settings: timezone, operating
   days/hours, valid starts, approval status/time, immutable versions, and any
   future calendar/closure exceptions. The authoritative owner and whether
   settings vary by campus/program remain undecided.
5. Section availability windows if the institution adopts them.
6. Explicit combined-class group and shared-meeting records linking participating
   offerings and their source requirements.
7. Scheduling-generation and timetable-version records containing UUID, term,
   immutable snapshot, snapshot version/hash, solver configuration/version,
   candidate/solver status, timestamps, result/diagnostics, retry/duplicate
   handling, approval/publication audit, historical-version linkage, and creator.
8. A durable association between candidate schedule rows and generation, source
   requirement, meeting instance, and occurrence number.
9. A source for authoritative enrollment where `subject_offerings.expected_students`
   is null and a clear rule for whether to use that field or current enrollments.
10. Generation-scope records/classification for the published timetable baseline,
    re-optimized published schedules, preserved drafts, explicitly replaced
    drafts, and separately approved fixed constraints; include atomic publication,
    historical version preservation, active room-allocation handling, and
    idempotent candidate persistence. Published schedules are not fixed by
    default.
11. CCS curriculum records if the institution wants the approved CCS
    minor-subject and specialization rule enforced or validated by code: a
    curriculum-to-subject requirement mapping per year level, a
    major/specialization entity, student specialization selection, and the
    linkage from curriculum requirements to subject offerings. Laravel now stores
    curriculum requirement mappings and uses them in readiness diagnostics; the
    authoritative student specialization source and program-major workflow remain
    unresolved, so the CCS rule is still not fully enforceable end to end.

### Remaining implementation sequence

1. **Implemented:** Laravel generation/snapshot records, deterministic input
   normalization, strict Python request/response models, semantic validation,
   synchronous solve, integer-minute CP-SAT adapter, independent result
   validation, authenticated Laravel HTTP dispatch, bounded timeouts, response
   validation, candidate persistence, and candidate revision history.
2. **Implemented:** production environment wiring for `SCHEDULER_URL`,
   `SCHEDULER_API_KEY`, timeout/cache settings, and explicit solver limits and
   objective weights. Render settings for the existing Python service are
   documented in `docs/deployment.md`.
3. **Remaining:** confirm configuration and plan approval ownership,
   candidate-approval authorization, draft replacement, and publication policy;
   approve objective weights and production solver limits where still pending.
4. **Remaining:** authorize candidate approval, atomically publish the selected
   candidate, retain the previous timetable version as active history, and
   transition only explicitly selected replacement drafts. Rejection or
   publication failure must leave the active timetable unchanged.
5. **Remaining:** establish the authoritative enrollment and student
   specialization sources, protected-commitment workflow, and any future
   asynchronous generation lifecycle required by measured production workloads.

## 12. Known limitations and decisions requiring confirmation

### Deferred

- Authorized approval/publication workflow, atomic active-timetable replacement,
  result-to-draft mapping, and any future queue/job lifecycle.
- Laravel v1.0 does not support section availability, explicit combined classes,
  fixed existing commitments, date-specific calendars, or replacement draft
  selection. Python v1.0 receives empty arrays for these unsupported inputs.
- One professor per ordinary scheduled meeting (Milestone 4.2) and one distinct
  Dean-approved professor per ordinary offering (Milestone 4.3) in version 1 are
  **APPROVED**. Multi-instructor meeting support, instructor-count/role
  requirements in meeting plans, and co-instruction workload crediting remain
  deferred and are not part of current requirements. No multi-instructor logic
  is implemented in current Python.
- Curriculum enforcement: the approved CCS minor-subject and specialization rule
  is only partially enforceable. Laravel stores curriculum requirement mappings
  and reports readiness mismatches; major/specialization entities, authoritative
  student selection, and complete curriculum-to-offering workflow remain future
  Laravel work (section 11, item 11).
- Complete-semester offering coverage validation (section 4.9) is implemented
  Laravel pre-send behavior. The required-set source is the term's existing open
  subject offerings with active section/subject/program; curriculum diagnostics
  are advisory until the authoritative specialization source is resolved.
- Ambiguity rejection for multiple distinct eligible professors on one ordinary
  offering (section 4.6) is implemented Laravel-side validation.
- Automatic regeneration hooks for mid-semester changes: none exist, and none
  are proposed. Manual changes use the existing Laravel workflow; regeneration
  is always an explicit authorized action.
- Cross-subject combined classes and combinations with differing meeting plans are
  deferred. Only explicit groups with matching requirements are represented here.
- Candidate/version persistence and revision history are implemented. Authorized
  approval, atomic active-timetable replacement, historical published-version
  retention, transactional stale-candidate rejection, and treatment of existing
  drafts remain future Laravel behavior. The approved full-semester
  re-optimization rule does not make published schedules fixed constraints.
  Python consumes the published baseline only for the soft placement-continuity
  objective; separately protected commitments remain unsupported.
- Date-specific calendars, holidays, split hybrid attendance, room setup/cleanup
  buffers, travel time, and multi-room meetings are unsupported.

### Unknown / institutional decisions

1. Which institutional body/role owns, authors, reviews, and approves time-grid
    configurations and subject meeting plans? Must author and approver be distinct?
2. Are time-grid settings institution-wide as proposed, or do they vary by
    campus/program? What is the approved IANA timezone?
3. What are each term's exact operating days, hours, and valid start boundaries?
    Are schedules recurring on every matching weekday, and how are holidays,
    campus closures, examinations, and one-off dates represented?
4. Who proposes offering-level meeting plans, who approves them, and what review
    workflow/version transition is required? What exact frequency/duration plans
    correspond to subject catalog hours?
5. Does the institutional "Dean" (or authorized academic personnel) who assigns
    professors to subject offerings map to an existing Laravel role
    (`administrator` / `department_personnel`, which currently create faculty
    assignments), or does assignment authorization require a different mapping?
    If multi-instructor meetings are ever adopted beyond the approved version 1
    rule, what instructor counts/roles would be required and how would teaching
    workload be credited?
6. How should existing drafts be handled during a full-semester candidate
    generation, which users may select drafts for replacement, and when should
    selected drafts transition? Preserved drafts are not fixed solver constraints
    by default; any protected-draft workflow requires separate approval.
7. Does a null room type truly permit every available type, including `office`, or
    will the institution approve an allowed-room set?
8. Which source is authoritative for enrollment when `expected_students` is null:
   current enrolled students, an administrator forecast, or a required manual
   value? How should combined enrollment be handled when counts are uncertain?
9. May lecture/lab duration be split or varied by occurrence?
10. Are part-time targets entered in hours or minutes, can they include fractions
   of a minute, and should under- and over-target deviations have equal cost?
11. Are full-time policy exceptions only additions to the standard days, as this
   contract and current synthetic model assume, or can they remove a standard day?
   Who authorizes them and how are policy effective dates/version history retained?
12. Should `preferred` windows remain hard positive-availability windows, as current
   Laravel publication checks require, while also receiving a soft preference?
13. Can the same professor hold multiple active assignment roles for one offering,
   and which exact assignment row is used for lecture versus laboratory meetings?
   The contract rejects ambiguous duplicate rows until decided.
14. Are combined classes restricted to identical subject and matching plans?
    May one explicit shared meeting satisfy differently titled requirements?
15. Does hybrid room capacity use total enrollment (the current Laravel rule) or
    only in-person attendees? No split count currently exists.
16. What objective weights and optional-meeting priorities are institutionally
    acceptable? Current Python weights are illustrative only.
17. What are the measured full-timetable solve durations, request/response sizes,
     maximum meeting count, concurrent-generation needs, and relevant HTTP and
     application timeout limits? This evidence determines whether a synchronous
     production boundary remains appropriate.
18. Which existing roles are the intended "authorized personnel" for every type
    of manual mid-semester schedule change, and is the existing
    draft/publish/cancel plus conflict resolve/waive behavior sufficient for all
    approved mid-semester change scenarios?
19. Which system or institutional record is the authoritative source for CCS
    curriculum requirements and student specialization (major) selection, and
    should curriculum data eventually live in Laravel (section 11, item 11) or
    be maintained externally and supplied through offering configuration?
20. How are previous semesters' schedules intended to be presented as historical
    records (existing published/cancelled data may suffice, or a dedicated
    archive view may be wanted)? No archive feature exists today.
21. Which authorized institutional role approves a complete candidate and
    authorizes publication? The contract does not assign that authority to a
    specific Dean, administrator, or other Laravel role.
22. What should happen when a currently published schedule belongs to an offering
    that is no longer in Laravel's authoritative required offering set? Generation
    is rejected rather than silently dropping it until this is decided.
23. What positive soft-objective weight for published-placement changes is
    institutionally appropriate, and should unmatched legacy baseline rows be
    backfilled with stable meeting-instance IDs? No weight/default is approved;
    unmapped baseline rows cannot be reliably compared.

## 13. Contract status

This file is the version 1.0 implementation contract. Its endpoint, fields,
validation procedures, data requirements, and communication behavior reflect the
current Python service and Laravel integration where marked implemented or
verified. The Python API exposes public liveness, authenticated readiness,
validation, and solve routes; Laravel remains the sole database authority.
Approval/publication workflow and several institutional decisions remain deferred
or unresolved as labeled.

**Revision (Milestone 4.3):** final scheduling contract corrections. Each
ordinary subject offering has exactly one distinct Dean-approved professor in
version 1, and multiple distinct eligible professors for one ordinary offering
are a Laravel-side ambiguity rejection. Whole-semester generation requires
complete offering coverage with pre-send validation and diagnostics. All
previously approved technical decisions and evidence labels are preserved.

**Revision (Milestone 4.4):** the full-semester AI re-optimization lifecycle is
approved: all required offerings, including those in the active published
timetable, are optimized into a candidate; published placements are not fixed by
default; Laravel preserves the active version until an authorized, atomic
publication replaces it and retains the prior version for history. Candidate
persistence and revision history are implemented. The approval role mapping,
atomic publication, and treatment of existing drafts remain unresolved.

**Revision (Milestone 4.5):** the configurable published-placement continuity
penalty is an approved soft objective. It compares day, start time, and room only
when a stable baseline meeting-instance link exists; it cannot override hard
constraints or change professor assignments. The adapter implements the objective;
the institutional weight remains an explicit environment decision and the sample
value is provisional.

**Revision (Milestone 5.2):** strict Python contract models, deterministic
semantic validation, the validation-only route, the solve endpoint, the CP-SAT
contract adapter, minute-based solving, independent result validation, Laravel
HTTP integration, response validation, and candidate persistence are implemented.

**Revision (Phase 2.2):** production connection settings, authenticated
readiness diagnostics, Render manual-service documentation, and contract
documentation alignment are implemented. Approval/publication workflow, duplicate
Render service creation, schema changes, and solver-algorithm changes remain out
of scope.

As of the current revision, the institutional rules in section 2
("Approved institutional scheduling rules"), the one-professor-per-meeting
version 1 rule, the one-distinct-professor-per-offering version 1 rule, and
full-semester re-optimization and minimal timetable change are **APPROVED**
business decisions; everything else remains PROPOSED, DEFERRED, or UNKNOWN as
labeled. The minimal-change weight/default is not approved.
