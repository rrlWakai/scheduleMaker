from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ortools.sat.python import cp_model

from ccsync_scheduler.academic import allowed_workdays, build_required_meetings
from ccsync_scheduler.models import (
    AcademicAssignment,
    AcademicProfessor,
    AcademicSchedulingProblem,
    AcademicSolveResult,
    EmploymentType,
    OptimizationMetrics,
    OptimizationWeights,
    RequiredClassMeeting,
    SolverStatus,
    TimeSlot,
)
from ccsync_scheduler.validation import validate_academic_assignments


_STATUS_BY_CODE = {
    cp_model.OPTIMAL: SolverStatus.OPTIMAL,
    cp_model.FEASIBLE: SolverStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
    cp_model.UNKNOWN: SolverStatus.UNKNOWN,
    cp_model.MODEL_INVALID: SolverStatus.MODEL_INVALID,
}


@dataclass(frozen=True)
class _Choice:
    meeting: RequiredClassMeeting
    professor_id: str
    room_id: str
    start: TimeSlot
    variable: cp_model.IntVar


def _status_for_code(code: int) -> SolverStatus:
    return _STATUS_BY_CODE.get(code, SolverStatus.UNKNOWN)


def _add_daily_gap_penalties(
    model: cp_model.CpModel,
    groups: dict[tuple[str, int], list[_Choice]],
    periods_per_day: int,
    prefix: str,
) -> list[cp_model.IntVar]:
    penalties: list[cp_model.IntVar] = []
    for (resource_id, day), choices in groups.items():
        used = model.NewBoolVar(f"{prefix}_{resource_id}_{day}_used")
        model.AddMaxEquality(used, [choice.variable for choice in choices])

        first_period = model.NewIntVar(
            0,
            periods_per_day,
            f"{prefix}_{resource_id}_{day}_first",
        )
        last_period = model.NewIntVar(
            0,
            periods_per_day,
            f"{prefix}_{resource_id}_{day}_last",
        )
        for choice in choices:
            model.Add(first_period <= choice.start.period).OnlyEnforceIf(choice.variable)
            model.Add(
                last_period >= choice.start.period + choice.meeting.duration_periods
            ).OnlyEnforceIf(choice.variable)

        model.Add(first_period == 0).OnlyEnforceIf(used.Not())
        model.Add(last_period == 0).OnlyEnforceIf(used.Not())
        daily_load = sum(
            choice.meeting.duration_periods * choice.variable for choice in choices
        )
        gap = model.NewIntVar(
            0,
            periods_per_day,
            f"{prefix}_{resource_id}_{day}_gap",
        )
        model.Add(gap == last_period - first_period - daily_load)
        penalties.append(gap)

    return penalties


def _compute_metrics(
    problem: AcademicSchedulingProblem,
    meetings: tuple[RequiredClassMeeting, ...],
    assignments: tuple[AcademicAssignment, ...],
) -> OptimizationMetrics:
    meeting_by_id = {meeting.id: meeting for meeting in meetings}
    professor_loads = {professor_id: 0 for professor_id in problem.professors}
    professor_day_intervals: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    section_day_intervals: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    room_waste = 0

    for assignment in assignments:
        meeting = meeting_by_id[assignment.meeting_id]
        start = assignment.start.period
        end = start + meeting.duration_periods
        professor_loads[assignment.professor_id] += meeting.duration_periods
        professor_day_intervals[(assignment.professor_id, assignment.start.day)].append(
            (start, end)
        )
        for section_id in meeting.section_ids:
            section_day_intervals[(section_id, assignment.start.day)].append((start, end))
        room_waste += (
            problem.rooms[assignment.room_id].capacity - meeting.expected_enrollment
        )

    def total_gaps(interval_groups: dict[tuple[str, int], list[tuple[int, int]]]) -> int:
        gap_total = 0
        for intervals in interval_groups.values():
            ordered = sorted(intervals)
            gap_total += sum(
                max(0, following_start - previous_end)
                for (_, previous_end), (following_start, _) in zip(
                    ordered,
                    ordered[1:],
                )
            )
        return gap_total

    part_time_deviation = sum(
        abs(professor_loads[professor.id] - professor.weekly_target_periods)
        for professor in problem.professors.values()
        if professor.employment_type is EmploymentType.PART_TIME
        and professor.weekly_target_periods is not None
    )
    full_time_loads = [
        professor_loads[professor.id]
        for professor in problem.professors.values()
        if professor.employment_type is EmploymentType.FULL_TIME
    ]
    full_time_spread = (
        max(full_time_loads) - min(full_time_loads) if full_time_loads else 0
    )
    section_daily_spread = 0
    for section_id in problem.sections:
        daily_loads = [
            sum(
                end - start
                for start, end in section_day_intervals.get((section_id, day), [])
            )
            for day in range(len(problem.day_names))
        ]
        if daily_loads:
            section_daily_spread += max(daily_loads) - min(daily_loads)

    return OptimizationMetrics(
        part_time_target_deviation_periods=part_time_deviation,
        full_time_workload_spread_periods=full_time_spread,
        professor_gap_periods=total_gaps(professor_day_intervals),
        section_gap_periods=total_gaps(section_day_intervals),
        room_capacity_waste_seats=room_waste,
        section_daily_load_spread_periods=section_daily_spread,
    )


def solve_academic(
    problem: AcademicSchedulingProblem,
    max_time_seconds: float = 5.0,
    objective_weights: OptimizationWeights = OptimizationWeights(),
) -> AcademicSolveResult:
    if max_time_seconds <= 0:
        raise ValueError("max_time_seconds must be greater than zero.")
    if any(
        weight < 0
        for weight in (
            objective_weights.part_time_target_deviation,
            objective_weights.full_time_load_spread,
            objective_weights.daily_gaps,
            objective_weights.room_capacity_waste,
            objective_weights.section_daily_spread,
        )
    ):
        raise ValueError("Optimization weights must be nonnegative.")

    meetings = build_required_meetings(problem)
    model = cp_model.CpModel()
    meeting_choices: dict[str, list[_Choice]] = defaultdict(list)
    professor_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    section_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    room_intervals: dict[tuple[str, int], list[cp_model.IntervalVar]] = defaultdict(list)
    professor_day_choices: dict[tuple[str, int], list[_Choice]] = defaultdict(list)
    section_day_choices: dict[tuple[str, int], list[_Choice]] = defaultdict(list)
    professor_load_terms: dict[str, list[cp_model.LinearExpr]] = defaultdict(list)
    section_day_load_terms: dict[tuple[str, int], list[cp_model.LinearExpr]] = defaultdict(list)
    room_waste_terms: list[cp_model.LinearExpr] = []

    for meeting in meetings:
        for professor_id in meeting.eligible_professor_ids:
            professor = problem.professors[professor_id]
            workdays = allowed_workdays(problem, professor_id)
            for day in sorted(workdays):
                for period in range(problem.periods_per_day - meeting.duration_periods + 1):
                    occupied = {
                        TimeSlot(day, slot_period)
                        for slot_period in range(period, period + meeting.duration_periods)
                    }
                    if not occupied.issubset(problem.available_slots):
                        continue
                    if not occupied.issubset(professor.available_slots):
                        continue
                    if any(
                        not occupied.issubset(problem.sections[section_id].available_slots)
                        for section_id in meeting.section_ids
                    ):
                        continue

                    for room in sorted(problem.rooms.values(), key=lambda item: item.id):
                        if room.capacity < meeting.expected_enrollment:
                            continue
                        if room.room_type != meeting.required_room_type:
                            continue

                        name = (
                            f"choose_{meeting.id}_{professor_id}_{room.id}_{day}_{period}"
                        )
                        variable = model.NewBoolVar(name)
                        choice = _Choice(
                            meeting=meeting,
                            professor_id=professor_id,
                            room_id=room.id,
                            start=TimeSlot(day, period),
                            variable=variable,
                        )
                        interval = model.NewOptionalFixedSizeIntervalVar(
                            period,
                            meeting.duration_periods,
                            variable,
                            f"{name}_interval",
                        )
                        meeting_choices[meeting.id].append(choice)
                        professor_intervals[(professor_id, day)].append(interval)
                        professor_day_choices[(professor_id, day)].append(choice)
                        professor_load_terms[professor_id].append(
                            meeting.duration_periods * variable
                        )
                        room_intervals[(room.id, day)].append(interval)
                        room_waste_terms.append(
                            (room.capacity - meeting.expected_enrollment) * variable
                        )
                        for section_id in meeting.section_ids:
                            section_intervals[(section_id, day)].append(interval)
                            section_day_choices[(section_id, day)].append(choice)
                            section_day_load_terms[(section_id, day)].append(
                                meeting.duration_periods * variable
                            )

        choices = meeting_choices[meeting.id]
        if choices:
            model.AddExactlyOne([choice.variable for choice in choices])
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

    professor_gaps = _add_daily_gap_penalties(
        model,
        professor_day_choices,
        problem.periods_per_day,
        "professor",
    )
    section_gaps = _add_daily_gap_penalties(
        model,
        section_day_choices,
        problem.periods_per_day,
        "section",
    )

    total_periods = sum(meeting.duration_periods for meeting in meetings)
    objective_terms: list[cp_model.LinearExpr] = []
    part_time_deviations: list[cp_model.IntVar] = []
    for professor in problem.professors.values():
        if professor.employment_type is not EmploymentType.PART_TIME:
            continue
        target = professor.weekly_target_periods
        if target is None:
            continue
        deviation = model.NewIntVar(
            0,
            total_periods + target,
            f"part_time_target_deviation_{professor.id}",
        )
        load = sum(professor_load_terms[professor.id])
        model.AddAbsEquality(deviation, load - target)
        part_time_deviations.append(deviation)

    full_time_loads = [
        sum(professor_load_terms[professor.id])
        for professor in problem.professors.values()
        if professor.employment_type is EmploymentType.FULL_TIME
    ]
    full_time_spread: cp_model.IntVar | None = None
    if len(full_time_loads) > 1:
        max_load = model.NewIntVar(0, total_periods, "full_time_max_load")
        min_load = model.NewIntVar(0, total_periods, "full_time_min_load")
        full_time_spread = model.NewIntVar(0, total_periods, "full_time_load_spread")
        model.AddMaxEquality(max_load, full_time_loads)
        model.AddMinEquality(min_load, full_time_loads)
        model.Add(full_time_spread == max_load - min_load)

    section_spreads: list[cp_model.IntVar] = []
    for section_id in problem.sections:
        daily_loads = [
            sum(section_day_load_terms[(section_id, day)])
            for day in range(len(problem.day_names))
        ]
        if len(daily_loads) < 2:
            continue
        max_load = model.NewIntVar(0, total_periods, f"{section_id}_max_daily_load")
        min_load = model.NewIntVar(0, total_periods, f"{section_id}_min_daily_load")
        spread = model.NewIntVar(0, total_periods, f"{section_id}_daily_load_spread")
        model.AddMaxEquality(max_load, daily_loads)
        model.AddMinEquality(min_load, daily_loads)
        model.Add(spread == max_load - min_load)
        section_spreads.append(spread)

    if objective_weights.part_time_target_deviation:
        objective_terms.extend(
            objective_weights.part_time_target_deviation * term
            for term in part_time_deviations
        )
    if objective_weights.full_time_load_spread and full_time_spread is not None:
        objective_terms.append(
            objective_weights.full_time_load_spread * full_time_spread
        )
    if objective_weights.daily_gaps:
        objective_terms.extend(
            objective_weights.daily_gaps * term
            for term in professor_gaps + section_gaps
        )
    if objective_weights.room_capacity_waste:
        objective_terms.extend(
            objective_weights.room_capacity_waste * term
            for term in room_waste_terms
        )
    if objective_weights.section_daily_spread:
        objective_terms.extend(
            objective_weights.section_daily_spread * term
            for term in section_spreads
        )
    if objective_terms:
        model.Minimize(sum(objective_terms))

    validation_error = model.Validate()
    if validation_error:
        return AcademicSolveResult(
            status=SolverStatus.MODEL_INVALID,
            details=validation_error,
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = 1
    status_code = solver.Solve(model)
    status = _status_for_code(status_code)
    if status not in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE):
        return AcademicSolveResult(
            status=status,
            solver_wall_time_seconds=solver.WallTime(),
        )

    assignments = tuple(
        AcademicAssignment(
            meeting_id=meeting.id,
            start=selected.start,
            professor_id=selected.professor_id,
            room_id=selected.room_id,
        )
        for meeting in meetings
        for selected in meeting_choices[meeting.id]
        if solver.BooleanValue(selected.variable)
    )
    errors = validate_academic_assignments(problem, assignments)
    if errors:
        raise RuntimeError(
            "CP-SAT returned an academic assignment that failed independent validation: "
            + "; ".join(errors)
        )

    return AcademicSolveResult(
        status=status,
        assignments=assignments,
        metrics=_compute_metrics(problem, meetings, assignments),
        solver_wall_time_seconds=solver.WallTime(),
    )
