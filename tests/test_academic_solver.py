from __future__ import annotations

from dataclasses import replace

from ccsync_scheduler.academic import build_required_meetings
from ccsync_scheduler.academic_solver import solve_academic
from ccsync_scheduler.demo import make_global_problem
from ccsync_scheduler.models import (
    AcademicProfessor,
    AcademicRoom,
    AcademicSchedulingProblem,
    AcademicSection,
    AcademicSubject,
    ApprovedProfessorAssignment,
    EmploymentType,
    Program,
    SolverStatus,
    SubjectOffering,
    TimeSlot,
    YearLevel,
)
from ccsync_scheduler.validation import validate_academic_assignments


def make_single_slot_problem(
    *,
    shared_professor: bool = False,
    shared_section: bool = False,
    single_room: bool = False,
    duration_periods: int = 1,
    periods_per_day: int = 1,
    approved: bool = True,
    room_capacity: int = 40,
    room_type: str = "general",
    professor_slots: frozenset[TimeSlot] | None = None,
) -> AcademicSchedulingProblem:
    slots = frozenset(
        TimeSlot(0, period) for period in range(periods_per_day)
    )
    professors = {
        "prof-1": AcademicProfessor(
            "prof-1",
            EmploymentType.FULL_TIME,
            slots if professor_slots is None else professor_slots,
        )
    }
    if not shared_professor:
        professors["prof-2"] = AcademicProfessor(
            "prof-2",
            EmploymentType.FULL_TIME,
            slots if professor_slots is None else professor_slots,
        )

    sections = {
        "section-1": AcademicSection("section-1", "program", "year-1", slots)
    }
    if not shared_section:
        sections["section-2"] = AcademicSection("section-2", "program", "year-1", slots)

    offerings = {
        "offering-1": SubjectOffering(
            "offering-1", "section-1", "subject-1", 1, 20, "general"
        ),
        "offering-2": SubjectOffering(
            "offering-2",
            "section-1" if shared_section else "section-2",
            "subject-2",
            1,
            20,
            "general",
        ),
    }
    rooms = {
        "room-1": AcademicRoom("room-1", room_capacity, room_type)
    }
    if not single_room:
        rooms["room-2"] = AcademicRoom("room-2", room_capacity, room_type)

    return AcademicSchedulingProblem(
        day_names=("Monday",),
        periods_per_day=periods_per_day,
        available_slots=slots,
        programs={"program": Program("program")},
        year_levels={"year-1": YearLevel("year-1", 1)},
        sections=sections,
        professors=professors,
        subjects={
            "subject-1": AcademicSubject("subject-1", duration_periods),
            "subject-2": AcademicSubject("subject-2", duration_periods),
        },
        offerings=offerings,
        professor_assignments=(
            ApprovedProfessorAssignment("offering-1", "prof-1", approved),
            ApprovedProfessorAssignment(
                "offering-2",
                "prof-1" if shared_professor else "prof-2",
                approved,
            ),
        ),
        rooms=rooms,
    )


def test_global_timetable_covers_programs_year_levels_and_sections() -> None:
    problem = make_global_problem()
    meetings = build_required_meetings(problem)

    result = solve_academic(problem)

    assert len(problem.programs) > 1
    assert len(problem.year_levels) > 1
    assert len(problem.sections) > 1
    assert result.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
    assert len(result.assignments) == len(meetings)
    assert validate_academic_assignments(problem, result.assignments) == []


def test_professor_conflict_is_infeasible() -> None:
    result = solve_academic(
        make_single_slot_problem(shared_professor=True, single_room=False)
    )

    assert result.status is SolverStatus.INFEASIBLE


def test_section_conflict_is_infeasible() -> None:
    result = solve_academic(
        make_single_slot_problem(shared_professor=False, shared_section=True)
    )

    assert result.status is SolverStatus.INFEASIBLE


def test_room_conflict_is_infeasible() -> None:
    result = solve_academic(
        make_single_slot_problem(
            shared_professor=False,
            shared_section=False,
            single_room=True,
        )
    )

    assert result.status is SolverStatus.INFEASIBLE


def test_professor_availability_is_a_hard_constraint() -> None:
    problem = make_single_slot_problem(professor_slots=frozenset())

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_room_capacity_is_a_hard_constraint() -> None:
    problem = make_single_slot_problem(
        single_room=True,
        room_capacity=10,
    )

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_room_type_is_a_hard_constraint() -> None:
    problem = make_single_slot_problem(
        single_room=True,
        room_type="lab",
    )

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_unapproved_professor_assignment_is_not_eligible() -> None:
    problem = make_single_slot_problem(approved=False)

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_combined_meeting_needs_a_professor_approved_for_every_offering() -> None:
    problem = make_global_problem()
    problem = replace(
        problem,
        professor_assignments=tuple(
            assignment
            for assignment in problem.professor_assignments
            if assignment.offering_id != "offer-cs-1a-ethics"
        ),
    )

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_multi_period_meeting_stays_within_one_day() -> None:
    problem = make_single_slot_problem(
        duration_periods=2,
        periods_per_day=2,
    )

    result = solve_academic(problem)

    assert result.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
    assert len(result.assignments) == 2
    assert all(assignment.start.period == 0 for assignment in result.assignments)
    assert validate_academic_assignments(problem, result.assignments) == []


def test_multi_period_meeting_cannot_cross_day_boundary() -> None:
    problem = make_single_slot_problem(
        duration_periods=2,
        periods_per_day=2,
    )
    slots = frozenset({TimeSlot(0, 1)})
    problem = replace(
        problem,
        day_names=("Monday", "Tuesday"),
        available_slots=slots,
        professors={
            key: replace(professor, available_slots=slots)
            for key, professor in problem.professors.items()
        },
        sections={
            key: replace(section, available_slots=slots)
            for key, section in problem.sections.items()
        },
    )

    result = solve_academic(problem)

    assert result.status is SolverStatus.INFEASIBLE


def test_full_time_weekday_rule_and_authorized_exception() -> None:
    problem = make_single_slot_problem()
    thursday = frozenset({TimeSlot(3, 0)})
    problem = replace(
        problem,
        day_names=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
        available_slots=thursday,
        professors={
            professor_id: replace(professor, available_slots=thursday)
            for professor_id, professor in problem.professors.items()
        },
        sections={
            section_id: replace(section, available_slots=thursday)
            for section_id, section in problem.sections.items()
        },
    )

    without_exception = solve_academic(problem)
    with_exception = replace(
        problem,
        professors={
            professor_id: replace(
                professor,
                authorized_workday_exception_days=frozenset({3}),
            )
            for professor_id, professor in problem.professors.items()
        },
    )
    with_exception_result = solve_academic(with_exception)

    assert without_exception.status is SolverStatus.INFEASIBLE
    assert with_exception_result.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)


def test_part_time_target_is_a_soft_objective() -> None:
    problem = make_single_slot_problem()
    part_time_problem = replace(
        problem,
        professors={
            professor_id: replace(
                professor,
                employment_type=EmploymentType.PART_TIME,
                weekly_target_periods=5,
            )
            for professor_id, professor in problem.professors.items()
        },
    )

    result = solve_academic(part_time_problem)

    assert result.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
    assert result.metrics is not None
    assert result.metrics.part_time_target_deviation_periods > 0


def test_same_subject_offerings_are_separate_unless_explicitly_combined() -> None:
    problem = make_global_problem()
    meetings = build_required_meetings(problem)
    combined_meeting = next(
        meeting for meeting in meetings if meeting.id.startswith("combined:")
    )
    separate_problem = replace(problem, combined_classes=())
    separate_meetings = build_required_meetings(separate_problem)

    assert len(combined_meeting.offering_ids) == 2
    assert len(combined_meeting.section_ids) == 2
    math_meetings = [
        meeting
        for meeting in meetings
        if any("math" in offering_id for offering_id in meeting.offering_ids)
    ]
    assert len(math_meetings) == 4
    assert all(len(meeting.offering_ids) == 1 for meeting in math_meetings)
    assert len(separate_meetings) == len(meetings) + 1
    assert all(
        len(meeting.offering_ids) == 1
        for meeting in separate_meetings
        if "ethics" in meeting.id
    )


def test_validator_detects_room_capacity_and_section_conflicts() -> None:
    problem = make_global_problem()
    result = solve_academic(problem)
    first, second = result.assignments[:2]
    invalid = (
        replace(first, room_id="room-general-35"),
        replace(second, start=first.start, room_id=first.room_id),
        *result.assignments[2:],
    )

    errors = validate_academic_assignments(problem, invalid)

    assert any("exceeds room capacity" in error for error in errors)
    assert "A section has overlapping meetings." in errors
