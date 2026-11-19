from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ortools.sat.python import cp_model
from pydantic import ValidationError

from ccsync_scheduler.contract import (
    Diagnostic,
    FullTimeDistribution,
    MeetingAssignment,
    ProfessorAssignmentResult,
    SolveRequest,
    SolveResponse,
    ValidationSummary,
    result_status_for,
    Warning,
    WorkloadMetric,
)
from ccsync_scheduler.contract_validation import (
    ContractIssue,
    MeetingSource,
    _availability_error,
    _fits_window,
    _minutes,
    meeting_sources_for_request,
    placement_metrics_for_assignments,
    validate_response_against_request,
)


logger = logging.getLogger("ccsync_scheduler.contract_solver")
_MAX_MINUTES_PER_DAY = 24 * 60


class ContractSolverError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _Candidate:
    meeting: MeetingSource
    day: int
    start: int
    end: int
    room_id: str | None
    variable: cp_model.IntVar
    preferred_window_miss: bool


def _wire_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _fits_fixed_commitments(
    request: SolveRequest,
    meeting: MeetingSource,
    day: int,
    start: int,
    end: int,
    room_id: str | None,
) -> bool:
    section_ids = set(meeting.section_ids)
    for commitment in request.existing_commitments:
        if commitment.day_of_week != day:
            continue
        commitment_start = _minutes(commitment.start_time)
        commitment_end = _minutes(commitment.end_time)
        if not (start < commitment_end and commitment_start < end):
            continue
        if meeting.professor_id in commitment.professor_ids:
            return False
        if section_ids.intersection(commitment.section_ids):
            return False
        if room_id is not None and room_id in {
            allocation.room_id for allocation in commitment.room_allocations
        }:
            return False
    return True


def _placement_candidates(
    request: SolveRequest,
    sources: dict[str, MeetingSource],
    model: cp_model.CpModel,
) -> dict[str, list[_Candidate]]:
    professors = {item.id: item for item in request.professors}
    sections = {item.id: item for item in request.sections}
    offerings = {item.id: item for item in request.offerings}
    rooms = {item.id: item for item in request.rooms}
    grids = {item.day_of_week: item for item in request.time_grid.days}
    policy = request.full_time_workday_policy
    exceptions = (
        {item.professor_id: set(item.authorized_additional_days) for item in policy.exceptions}
        if policy is not None
        else {}
    )
    standard_days = set(policy.standard_days) if policy is not None else set()
    preferred_windows_by_professor = {
        item.id: [
            window
            for window in item.availability_windows
            if window.availability_type == "preferred"
        ]
        for item in request.professors
    }

    candidates: dict[str, list[_Candidate]] = {meeting_id: [] for meeting_id in sources}
    next_variable = 0
    for meeting_id in sorted(sources):
        meeting = sources[meeting_id]
        professor = professors[meeting.professor_id]
        allowed_days = set(meeting.allowed_days)
        if professor.employment_type == "full_time":
            allowed_days.intersection_update(
                standard_days | exceptions.get(professor.id, set())
            )

        enrollment = sum(
            offerings[offering_id].expected_enrollment or 0
            for offering_id in meeting.offering_ids
        )
        for day in sorted(allowed_days):
            grid = grids.get(day)
            if grid is None:
                continue
            opening = _minutes(grid.operating_start)
            closing = _minutes(grid.operating_end)
            start_times = meeting.valid_start_times or tuple(grid.valid_start_times)
            for start_time in start_times:
                start = _minutes(start_time)
                end = start + meeting.duration_minutes
                if start < opening or end > closing:
                    continue
                if _availability_error(
                    start,
                    end,
                    day,
                    professor.availability_windows,
                ):
                    continue
                if any(
                    section.availability_windows
                    and not _fits_window(
                        start,
                        end,
                        section.availability_windows,
                        day,
                    )
                    for section in (sections[item] for item in meeting.section_ids)
                ):
                    continue

                if meeting.modality == "online":
                    room_options: Iterable[str | None] = (None,)
                else:
                    room_options = (
                        room.id
                        for room in sorted(request.rooms, key=lambda item: item.id)
                        if room.capacity >= enrollment
                        and (
                            meeting.required_room_type is None
                            or room.room_type == meeting.required_room_type
                        )
                    )
                preferred_windows = preferred_windows_by_professor[professor.id]
                preferred_miss = bool(preferred_windows) and not any(
                    window.day_of_week == day
                    and _minutes(window.start_time) <= start
                    and end <= _minutes(window.end_time)
                    for window in preferred_windows
                )
                for room_id in room_options:
                    if not _fits_fixed_commitments(
                        request,
                        meeting,
                        day,
                        start,
                        end,
                        room_id,
                    ):
                        continue
                    variable = model.NewBoolVar(f"placement_{next_variable}")
                    next_variable += 1
                    candidates[meeting_id].append(
                        _Candidate(
                            meeting=meeting,
                            day=day,
                            start=start,
                            end=end,
                            room_id=room_id,
                            variable=variable,
                            preferred_window_miss=preferred_miss,
                        )
                    )
    return candidates


def _add_daily_gap_terms(
    model: cp_model.CpModel,
    groups: dict[tuple[str, int], list[_Candidate]],
    prefix: str,
) -> list[cp_model.IntVar]:
    penalties: list[cp_model.IntVar] = []
    for index, choices in enumerate(groups.values()):
        if not choices:
            continue
        used = model.NewBoolVar(f"{prefix}_{index}_used")
        model.AddMaxEquality(used, [item.variable for item in choices])
        first = model.NewIntVar(0, _MAX_MINUTES_PER_DAY, f"{prefix}_{index}_first")
        last = model.NewIntVar(0, _MAX_MINUTES_PER_DAY, f"{prefix}_{index}_last")
        for choice in choices:
            model.Add(first <= choice.start).OnlyEnforceIf(choice.variable)
            model.Add(last >= choice.end).OnlyEnforceIf(choice.variable)
        model.Add(first == 0).OnlyEnforceIf(used.Not())
        model.Add(last == 0).OnlyEnforceIf(used.Not())
        duration = sum(
            choice.meeting.duration_minutes * choice.variable
            for choice in choices
        )
        gap = model.NewIntVar(0, _MAX_MINUTES_PER_DAY, f"{prefix}_{index}_gap")
        model.Add(gap == last - first - duration)
        penalties.append(gap)
    return penalties


def _group_choices(
    choices: dict[str, list[_Candidate]],
    resource_for: str,
) -> dict[tuple[str, int], list[_Candidate]]:
    groups: dict[tuple[str, int], list[_Candidate]] = defaultdict(list)
    for meeting_choices in choices.values():
        for choice in meeting_choices:
            resource_ids = (
                [choice.meeting.professor_id]
                if resource_for == "professor"
                else list(choice.meeting.section_ids)
            )
            for resource_id in resource_ids:
                groups[(resource_id, choice.day)].append(choice)
    return groups


def _add_objective(
    request: SolveRequest,
    sources: dict[str, MeetingSource],
    choices: dict[str, list[_Candidate]],
    model: cp_model.CpModel,
) -> bool:
    weights = request.solver_config.objective_weights
    objective: list[cp_model.LinearExpr | int] = []

    if weights.optional_meeting_omission:
        for meeting_id in sorted(sources):
            meeting = sources[meeting_id]
            if meeting.mandatory:
                continue
            selected = sum(item.variable for item in choices[meeting_id])
            objective.append(
                weights.optional_meeting_omission * (1 - selected)
            )

    if weights.preferred_window_miss_per_meeting:
        objective.extend(
            weights.preferred_window_miss_per_meeting * item.variable
            for meeting_choices in choices.values()
            for item in meeting_choices
            if item.preferred_window_miss
        )

    if weights.published_placement_change_per_meeting:
        baseline_by_meeting: dict[str, list[object]] = defaultdict(list)
        for baseline in request.published_timetable_baseline:
            if baseline.meeting_instance_id is not None:
                baseline_by_meeting[baseline.meeting_instance_id].append(baseline)
        for index, meeting_id in enumerate(sorted(baseline_by_meeting)):
            rows = baseline_by_meeting[meeting_id]
            if meeting_id not in sources or not rows:
                continue
            first = rows[0]
            if any(
                row.day_of_week != first.day_of_week
                or row.start_time != first.start_time
                or tuple(sorted(item.room_id for item in row.room_allocations))
                != tuple(sorted(item.room_id for item in first.room_allocations))
                for row in rows[1:]
            ):
                continue
            if len(first.room_allocations) > 1:
                continue
            prior_room = (
                first.room_allocations[0].room_id
                if first.room_allocations
                else None
            )
            retained_options = [
                item.variable
                for item in choices[meeting_id]
                if item.day == first.day_of_week
                and _wire_time(item.start) == first.start_time
                and item.room_id == prior_room
            ]
            changed = model.NewBoolVar(f"placement_changed_{index}")
            model.Add(changed + sum(retained_options) == 1)
            objective.append(
                weights.published_placement_change_per_meeting * changed
            )

    professors = {item.id: item for item in request.professors}
    professor_choices = _group_choices(choices, "professor")
    professor_load_terms: dict[str, list[cp_model.LinearExpr]] = defaultdict(list)
    max_professor_load: dict[str, int] = defaultdict(int)
    for meeting_id, meeting_choices in choices.items():
        meeting = sources[meeting_id]
        if not meeting_choices:
            continue
        for choice in meeting_choices:
            professor_load_terms[meeting.professor_id].append(
                meeting.duration_minutes * choice.variable
            )
        max_professor_load[meeting.professor_id] += meeting.duration_minutes

    if weights.part_time_target_deviation_per_minute:
        for professor in sorted(request.professors, key=lambda item: item.id):
            if professor.employment_type != "part_time":
                continue
            target = professor.part_time_weekly_target_minutes
            assert target is not None
            upper = max(max_professor_load[professor.id], target)
            deviation = model.NewIntVar(
                0,
                upper,
                f"part_time_deviation_{professor.id}",
            )
            load = sum(professor_load_terms[professor.id])
            model.AddAbsEquality(deviation, load - target)
            objective.append(weights.part_time_target_deviation_per_minute * deviation)

    full_time_professors = [
        item
        for item in request.professors
        if item.employment_type == "full_time"
    ]
    if weights.full_time_load_spread_per_minute and len(full_time_professors) > 1:
        upper = max(
            (max_professor_load[item.id] for item in full_time_professors),
            default=0,
        )
        loads = [
            sum(professor_load_terms[item.id])
            for item in full_time_professors
        ]
        maximum = model.NewIntVar(0, upper, "full_time_max_load")
        minimum = model.NewIntVar(0, upper, "full_time_min_load")
        spread = model.NewIntVar(0, upper, "full_time_load_spread")
        model.AddMaxEquality(maximum, loads)
        model.AddMinEquality(minimum, loads)
        model.Add(spread == maximum - minimum)
        objective.append(weights.full_time_load_spread_per_minute * spread)

    if weights.professor_gap_per_minute:
        objective.extend(
            weights.professor_gap_per_minute
            * item
            for item in _add_daily_gap_terms(
                model,
                professor_choices,
                "professor_gap",
            )
        )

    if weights.section_gap_per_minute:
        objective.extend(
            weights.section_gap_per_minute
            * item
            for item in _add_daily_gap_terms(
                model,
                _group_choices(choices, "section"),
                "section_gap",
            )
        )

    if weights.room_capacity_waste_per_seat:
        rooms = {item.id: item for item in request.rooms}
        offerings = {item.id: item for item in request.offerings}
        for meeting_choices in choices.values():
            for choice in meeting_choices:
                if choice.room_id is None:
                    continue
                enrollment = sum(
                    offerings[item].expected_enrollment or 0
                    for item in choice.meeting.offering_ids
                )
                waste = rooms[choice.room_id].capacity - enrollment
                objective.append(weights.room_capacity_waste_per_seat * waste * choice.variable)

    if weights.section_daily_load_spread_per_minute:
        days = [item.day_of_week for item in request.time_grid.days]
        section_load_terms: dict[tuple[str, int], list[cp_model.LinearExpr]] = defaultdict(list)
        section_upper: dict[str, int] = defaultdict(int)
        for meeting_id, meeting_choices in choices.items():
            meeting = sources[meeting_id]
            for choice in meeting_choices:
                for section_id in meeting.section_ids:
                    section_load_terms[(section_id, choice.day)].append(
                        meeting.duration_minutes * choice.variable
                    )
            if meeting_choices:
                for section_id in meeting.section_ids:
                    section_upper[section_id] += meeting.duration_minutes
        for section in sorted(request.sections, key=lambda item: item.id):
            if len(days) < 2:
                continue
            upper = section_upper[section.id]
            loads = [
                sum(section_load_terms[(section.id, day)])
                for day in days
            ]
            maximum = model.NewIntVar(0, upper, f"section_{section.id}_max_day")
            minimum = model.NewIntVar(0, upper, f"section_{section.id}_min_day")
            spread = model.NewIntVar(0, upper, f"section_{section.id}_day_spread")
            model.AddMaxEquality(maximum, loads)
            model.AddMinEquality(minimum, loads)
            model.Add(spread == maximum - minimum)
            objective.append(
                weights.section_daily_load_spread_per_minute * spread
            )

    if not objective:
        return False
    model.Minimize(sum(objective))
    return True


def _add_hard_constraints(
    request: SolveRequest,
    sources: dict[str, MeetingSource],
    choices: dict[str, list[_Candidate]],
    model: cp_model.CpModel,
) -> None:
    for meeting_id, meeting_choices in choices.items():
        variables = [item.variable for item in meeting_choices]
        if sources[meeting_id].mandatory:
            if variables:
                model.AddExactlyOne(variables)
            else:
                model.AddBoolOr([])
        elif variables:
            model.AddAtMostOne(variables)

    offering_choices: dict[str, list[cp_model.IntVar]] = {
        item.id: [] for item in request.offerings
    }
    for meeting_id, meeting_choices in choices.items():
        for choice in meeting_choices:
            for offering_id in sources[meeting_id].offering_ids:
                offering_choices[offering_id].append(choice.variable)
    for variables in offering_choices.values():
        if variables:
            model.Add(sum(variables) >= 1)
        else:
            model.AddBoolOr([])

    professor_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    section_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    room_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    interval_index = 0
    for meeting_id in sorted(choices):
        for choice in choices[meeting_id]:
            interval = model.NewOptionalFixedSizeIntervalVar(
                choice.start,
                choice.meeting.duration_minutes,
                choice.variable,
                f"candidate_interval_{interval_index}",
            )
            interval_index += 1
            professor_intervals[(choice.meeting.professor_id, choice.day)].append(interval)
            for section_id in choice.meeting.section_ids:
                section_intervals[(section_id, choice.day)].append(interval)
            if choice.room_id is not None:
                room_intervals[(choice.room_id, choice.day)].append(interval)

    for groups in (professor_intervals, section_intervals, room_intervals):
        for intervals in groups.values():
            if len(intervals) > 1:
                model.AddNoOverlap(intervals)


def _diagnostic_for_missing_choices(
    meeting: MeetingSource,
) -> Diagnostic:
    return Diagnostic(
        code="NO_VALID_PLACEMENT",
        message=(
            "No placement satisfies the submitted time grid, availability, "
            "room, and fixed-commitment requirements for this meeting."
        ),
        evidence="input_check",
        meeting_requirement_ids=[meeting.requirement_id],
        related_entity_ids=sorted(
            {
                meeting.instance_id,
                meeting.professor_id,
                *meeting.offering_ids,
                *meeting.section_ids,
            }
        ),
    )


def _no_candidate_response(
    request: SolveRequest,
    sources: dict[str, MeetingSource],
    status: str,
    started_at: str,
    completed_at: str,
    elapsed_ms: int,
    diagnostics: list[Diagnostic],
    *,
    result_status: str,
    time_limit_reached: bool,
) -> SolveResponse:
    mandatory_count = sum(item.mandatory for item in sources.values())
    return SolveResponse(
        contract_version=request.contract_version,
        generation_id=request.metadata.generation_id,
        academic_term_id=request.academic_term.id,
        snapshot_sha256=request.metadata.snapshot_sha256,
        correlation_id=request.metadata.correlation_id,
        solver_status=status,
        result_status=result_status,
        started_at=started_at,
        completed_at=completed_at,
        solver_execution_ms=elapsed_ms,
        objective_value=None,
        best_bound=None,
        optimality_proven=False,
        time_limit_reached=time_limit_reached,
        assignments=[],
        validation_summary=ValidationSummary(
            independent_validation_status="not_run",
            scheduled_meeting_count=0,
            required_mandatory_meeting_count=mandatory_count,
            desired_meeting_count=len(sources),
            required_offering_count=len(request.offerings),
            represented_offering_count=0,
            complete_candidate=False,
            validation_errors=[],
        ),
        unscheduled_mandatory_meeting_ids=None,
        diagnostics=diagnostics,
        workload_metrics=[],
        full_time_distribution=None,
        placement_metrics={
            "comparable_meeting_count": 0,
            "retained_placement_count": 0,
            "changed_placement_count": 0,
            "unmatched_baseline_schedule_count": 0,
        },
        warnings=[],
    )


def solve_contract_request(request: SolveRequest) -> SolveResponse:
    sources = meeting_sources_for_request(request)
    model = cp_model.CpModel()
    choices = _placement_candidates(request, sources, model)
    _add_hard_constraints(request, sources, choices, model)
    has_objective = _add_objective(request, sources, choices, model)

    invalid_model = model.Validate()
    if invalid_model:
        logger.error("CP-SAT rejected the generated model: %s", invalid_model)
        raise ContractSolverError(
            "solver_model_invalid",
            "CP-SAT rejected the generated model.",
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = request.solver_config.time_limit_ms / 1000
    solver.parameters.num_search_workers = request.solver_config.worker_count
    solver.parameters.random_seed = request.solver_config.random_seed

    started = datetime.now(timezone.utc)
    started_at = _timestamp(started)
    status_code = solver.Solve(model)
    completed_at = _timestamp(datetime.now(timezone.utc))
    wall_ms = max(0.0, solver.WallTime() * 1000)
    elapsed_ms = round(wall_ms)
    status = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
    }.get(status_code)
    if status is None:
        raise ContractSolverError(
            "solver_model_invalid",
            "CP-SAT did not return a supported solve status.",
        )
    time_limit_reached = (
        status != "OPTIMAL" and wall_ms >= request.solver_config.time_limit_ms
    )
    result_status = result_status_for(status, time_limit_reached)

    if status in {"INFEASIBLE", "UNKNOWN"}:
        diagnostics = [
            _diagnostic_for_missing_choices(sources[meeting_id])
            for meeting_id in sorted(sources)
            if not choices[meeting_id]
        ]
        if status == "INFEASIBLE":
            diagnostics.append(
                Diagnostic(
                    code="MODEL_INFEASIBLE",
                    message=(
                        "CP-SAT proved that no assignment satisfies all submitted "
                        "hard constraints."
                    ),
                    evidence="solver_proven",
                    meeting_requirement_ids=[],
                    related_entity_ids=[],
                )
            )
        elif time_limit_reached:
            diagnostics.append(
                Diagnostic(
                    code="TIME_LIMIT_NO_CANDIDATE",
                    message=(
                        "The solver reached its configured time limit without "
                        "returning a candidate schedule."
                    ),
                    evidence="solver_proven",
                    meeting_requirement_ids=[],
                    related_entity_ids=[],
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    code="UNKNOWN_NO_CANDIDATE",
                    message=(
                        "CP-SAT returned UNKNOWN before the configured time limit "
                        "without returning a candidate schedule."
                    ),
                    evidence="heuristic",
                    meeting_requirement_ids=[],
                    related_entity_ids=[],
                )
            )
        return _no_candidate_response(
            request,
            sources,
            status,
            started_at,
            completed_at,
            elapsed_ms,
            diagnostics,
            result_status=result_status,
            time_limit_reached=time_limit_reached,
        )

    selected = [
        choice
        for meeting_id in sorted(choices)
        for choice in choices[meeting_id]
        if solver.BooleanValue(choice.variable)
    ]
    assignment_rows = [
        MeetingAssignment(
            meeting_instance_id=choice.meeting.instance_id,
            source_requirement_id=choice.meeting.requirement_id,
            occurrence_number=choice.meeting.occurrence,
            subject_offering_ids=list(choice.meeting.offering_ids),
            section_ids=list(choice.meeting.section_ids),
            professor_id=choice.meeting.professor_id,
            professor_assignments=[
                ProfessorAssignmentResult(
                    subject_offering_id=offering_id,
                    faculty_assignment_id=assignment_id,
                    assignment_role=role,
                )
                for offering_id, assignment_id, role in choice.meeting.faculty_assignments
            ],
            is_primary=True,
            room_id=choice.room_id,
            meeting_type=choice.meeting.meeting_type,
            modality=choice.meeting.modality,
            day_of_week=choice.day,
            start_time=_wire_time(choice.start),
            end_time=_wire_time(choice.end),
            duration_minutes=choice.meeting.duration_minutes,
        )
        for choice in selected
    ]
    assignment_rows.sort(key=lambda item: item.meeting_instance_id)

    sources_by_id = {item.id: item for item in request.professors}
    workloads = {professor_id: 0 for professor_id in sources_by_id}
    for assignment in assignment_rows:
        workloads[assignment.professor_id] += assignment.duration_minutes
    workload_metrics = []
    for professor_id, professor in sorted(sources_by_id.items()):
        assigned = workloads[professor_id]
        target = professor.part_time_weekly_target_minutes
        if professor.employment_type == "part_time":
            assert target is not None
            deviation = assigned - target
            workload_metrics.append(
                WorkloadMetric(
                    professor_id=professor_id,
                    employment_type=professor.employment_type,
                    assigned_teaching_minutes=assigned,
                    weekly_target_minutes=target,
                    signed_target_deviation_minutes=deviation,
                    absolute_target_deviation_minutes=abs(deviation),
                )
            )
        else:
            workload_metrics.append(
                WorkloadMetric(
                    professor_id=professor_id,
                    employment_type=professor.employment_type,
                    assigned_teaching_minutes=assigned,
                    weekly_target_minutes=None,
                    signed_target_deviation_minutes=None,
                    absolute_target_deviation_minutes=None,
                )
            )

    full_time_loads = [
        workloads[item.id]
        for item in sources_by_id.values()
        if item.employment_type == "full_time"
    ]
    full_time_distribution = (
        FullTimeDistribution(
            minimum_assigned_minutes=min(full_time_loads),
            maximum_assigned_minutes=max(full_time_loads),
            spread_minutes=max(full_time_loads) - min(full_time_loads),
        )
        if full_time_loads
        else None
    )
    placement_metrics = placement_metrics_for_assignments(
        request,
        assignment_rows,
        sources,
    )

    warnings: list[Warning] = []
    for choice in selected:
        if choice.preferred_window_miss:
            warnings.append(
                Warning(
                    code="PREFERRED_WINDOW_MISSED",
                    message="The selected placement is outside the professor's preferred window.",
                    related_entity_ids=[choice.meeting.instance_id, choice.meeting.professor_id],
                )
            )
    selected_ids = {item.meeting_instance_id for item in assignment_rows}
    for meeting_id in sorted(sources):
        meeting = sources[meeting_id]
        if not meeting.mandatory and meeting_id not in selected_ids:
            warnings.append(
                Warning(
                    code="OPTIONAL_MEETING_OMITTED",
                    message="The optional meeting occurrence was not scheduled.",
                    related_entity_ids=[meeting_id, *meeting.offering_ids],
                )
            )
    for metric in workload_metrics:
        if metric.employment_type == "part_time" and metric.absolute_target_deviation_minutes:
            warnings.append(
                Warning(
                    code="PART_TIME_TARGET_DEVIATION",
                    message=(
                        "Assigned teaching minutes differ from the professor's "
                        "soft weekly target."
                    ),
                    related_entity_ids=[metric.professor_id],
                )
            )
    if status == "FEASIBLE":
        warnings.append(
            Warning(
                code="OPTIMALITY_NOT_PROVEN",
                message="A valid complete candidate was found, but optimality was not proven.",
                related_entity_ids=[],
            )
        )

    represented = {
        offering_id
        for assignment in assignment_rows
        for offering_id in assignment.subject_offering_ids
    }
    response_data = {
        "contract_version": request.contract_version,
        "generation_id": request.metadata.generation_id,
        "academic_term_id": request.academic_term.id,
        "snapshot_sha256": request.metadata.snapshot_sha256,
        "correlation_id": request.metadata.correlation_id,
        "solver_status": status,
        "result_status": result_status,
        "started_at": started_at,
        "completed_at": completed_at,
        "solver_execution_ms": elapsed_ms,
        "objective_value": solver.ObjectiveValue() if has_objective else None,
        "best_bound": solver.BestObjectiveBound() if has_objective else None,
        "optimality_proven": status == "OPTIMAL",
        "time_limit_reached": time_limit_reached,
        "assignments": assignment_rows,
        "validation_summary": {
            "independent_validation_status": "passed",
            "scheduled_meeting_count": len(assignment_rows),
            "required_mandatory_meeting_count": sum(
                item.mandatory for item in sources.values()
            ),
            "desired_meeting_count": len(sources),
            "required_offering_count": len(request.offerings),
            "represented_offering_count": len(represented),
            "complete_candidate": True,
            "validation_errors": [],
        },
        "unscheduled_mandatory_meeting_ids": [],
        "diagnostics": [],
        "workload_metrics": workload_metrics,
        "full_time_distribution": full_time_distribution,
        "placement_metrics": placement_metrics,
        "warnings": warnings,
    }
    try:
        response = SolveResponse.model_validate(response_data)
    except ValidationError as exc:
        raise ContractSolverError(
            "solver_response_invalid",
            "The solver generated a response that failed contract validation.",
        ) from exc

    validation_issues: list[ContractIssue] = validate_response_against_request(
        request,
        response,
    )
    if validation_issues:
        logger.error(
            "Independent validation rejected generated candidate: %s",
            [issue.code for issue in validation_issues],
        )
        raise ContractSolverError(
            "independent_validation_failed",
            "Independent candidate validation failed.",
        )
    return response
