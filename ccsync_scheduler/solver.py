from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from ccsync_scheduler.models import (
    ScheduledSession,
    SchedulingProblem,
    SolveResult,
    SolverStatus,
    TimeSlot,
)
from ccsync_scheduler.validation import validate_assignments


_STATUS_BY_CODE = {
    cp_model.OPTIMAL: SolverStatus.OPTIMAL,
    cp_model.FEASIBLE: SolverStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
    cp_model.UNKNOWN: SolverStatus.UNKNOWN,
    cp_model.MODEL_INVALID: SolverStatus.MODEL_INVALID,
}


def _status_for_code(code: int) -> SolverStatus:
    return _STATUS_BY_CODE.get(code, SolverStatus.UNKNOWN)


def _validate_problem_references(problem: SchedulingProblem) -> None:
    if problem.days < 1 or problem.periods_per_day < 1:
        raise ValueError("The problem must define at least one day and one period per day.")

    session_ids = [session.id for session in problem.required_sessions]
    if len(session_ids) != len(set(session_ids)):
        raise ValueError("Required session IDs must be unique.")

    for session in problem.required_sessions:
        if session.professor_id not in problem.professors:
            raise ValueError(f"Unknown professor {session.professor_id!r}.")
        if session.section_id not in problem.sections:
            raise ValueError(f"Unknown section {session.section_id!r}.")
        subject = problem.subjects.get(session.subject_id)
        if subject is None:
            raise ValueError(f"Unknown subject {session.subject_id!r}.")
        if not 1 <= subject.duration_periods <= problem.periods_per_day:
            raise ValueError(f"Subject {subject.id!r} has an invalid meeting duration.")
        unknown_rooms = set(session.eligible_room_ids) - problem.rooms.keys()
        if unknown_rooms:
            raise ValueError(f"Unknown eligible rooms: {sorted(unknown_rooms)}.")


def _valid_start_values(problem: SchedulingProblem, session_id: str) -> list[int]:
    session = next(item for item in problem.required_sessions if item.id == session_id)
    duration = problem.subjects[session.subject_id].duration_periods
    professor_slots = problem.professors[session.professor_id].available_slots
    section_slots = problem.sections[session.section_id].available_slots
    starts: list[int] = []

    for day in range(problem.days):
        for period in range(problem.periods_per_day - duration + 1):
            occupied = {
                TimeSlot(day, slot_period)
                for slot_period in range(period, period + duration)
            }
            if (
                occupied.issubset(problem.available_slots)
                and occupied.issubset(professor_slots)
                and occupied.issubset(section_slots)
            ):
                starts.append(day * problem.periods_per_day + period)

    return starts


def solve(problem: SchedulingProblem, max_time_seconds: float = 2.0) -> SolveResult:
    _validate_problem_references(problem)
    if max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be greater than zero.")

    model = cp_model.CpModel()
    starts: dict[str, cp_model.IntVar] = {}
    ends: dict[str, cp_model.IntVar] = {}
    room_choices: dict[str, dict[str, cp_model.IntVar]] = {}
    professor_intervals: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    section_intervals: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    room_intervals: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    total_periods = problem.days * problem.periods_per_day

    for session in problem.required_sessions:
        duration = problem.subjects[session.subject_id].duration_periods
        start = model.NewIntVar(0, total_periods - 1, f"{session.id}_start")
        end = model.NewIntVar(1, total_periods, f"{session.id}_end")
        starts[session.id] = start
        ends[session.id] = end
        model.Add(end == start + duration)

        valid_starts = _valid_start_values(problem, session.id)
        if valid_starts:
            model.AddAllowedAssignments([start], [(value,) for value in valid_starts])
        else:
            model.AddBoolOr([])

        interval = model.NewIntervalVar(
            start,
            duration,
            end,
            f"{session.id}_resource_interval",
        )
        professor_intervals[session.professor_id].append(interval)
        section_intervals[session.section_id].append(interval)

        choices: dict[str, cp_model.IntVar] = {}
        for room_id in sorted(set(session.eligible_room_ids)):
            choice = model.NewBoolVar(f"{session.id}_room_{room_id}")
            choices[room_id] = choice
            room_intervals[room_id].append(
                model.NewOptionalIntervalVar(
                    start,
                    duration,
                    end,
                    choice,
                    f"{session.id}_{room_id}_interval",
                )
            )
        room_choices[session.id] = choices

        if choices:
            model.AddExactlyOne(list(choices.values()))
        else:
            model.AddBoolOr([])

    for intervals in professor_intervals.values():
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)
    for intervals in section_intervals.values():
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)
    for intervals in room_intervals.values():
        if len(intervals) > 1:
            model.AddNoOverlap(intervals)

    if starts:
        model.Minimize(sum(starts.values()))

    validation_error = model.Validate()
    if validation_error:
        return SolveResult(SolverStatus.MODEL_INVALID, details=validation_error)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = 1
    status_code = solver.Solve(model)
    status = _status_for_code(status_code)

    if status not in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE):
        return SolveResult(status)

    assignments = tuple(
        ScheduledSession(
            session_id=session.id,
            start=TimeSlot(
                day=solver.Value(starts[session.id]) // problem.periods_per_day,
                period=solver.Value(starts[session.id]) % problem.periods_per_day,
            ),
            room_id=next(
                room_id
                for room_id, choice in room_choices[session.id].items()
                if solver.BooleanValue(choice)
            ),
        )
        for session in problem.required_sessions
    )
    errors = validate_assignments(problem, assignments)
    if errors:
        raise RuntimeError(
            "CP-SAT returned an assignment that failed independent validation: "
            + "; ".join(errors)
        )

    return SolveResult(status, assignments)
