from __future__ import annotations

from ccsync_scheduler.models import (
    AcademicSchedulingProblem,
    EmploymentType,
    RequiredClassMeeting,
    TimeSlot,
)

STANDARD_FULL_TIME_WORKDAY_NAMES = frozenset({"Monday", "Tuesday", "Wednesday"})


def allowed_workdays(problem: AcademicSchedulingProblem, professor_id: str) -> frozenset[int]:
    professor = problem.professors[professor_id]
    if professor.employment_type is EmploymentType.FULL_TIME:
        standard_days = {
            index
            for index, name in enumerate(problem.day_names)
            if name in STANDARD_FULL_TIME_WORKDAY_NAMES
        }
        return frozenset(standard_days.union(professor.authorized_workday_exception_days))
    return frozenset(range(len(problem.day_names)))


def _validate_problem(problem: AcademicSchedulingProblem) -> None:
    if not problem.day_names or len(set(problem.day_names)) != len(problem.day_names):
        raise ValueError("The timetable must define unique day names.")
    valid_day_names = {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
    if any(day_name not in valid_day_names for day_name in problem.day_names):
        raise ValueError("Timetable day names must be full weekday names.")
    if problem.periods_per_day < 1:
        raise ValueError("The timetable must define at least one period per day.")

    def validate_slots(slots: frozenset[TimeSlot], label: str) -> None:
        if any(
            not 0 <= slot.day < len(problem.day_names)
            or not 0 <= slot.period < problem.periods_per_day
            for slot in slots
        ):
            raise ValueError(f"{label} contains a time slot outside the timetable.")

    validate_slots(problem.available_slots, "Global availability")

    for program_id, program in problem.programs.items():
        if program_id != program.id:
            raise ValueError("Program dictionary keys must match program IDs.")

    for year_level_id, year_level in problem.year_levels.items():
        if year_level_id != year_level.id or year_level.level < 1:
            raise ValueError("Year levels need matching IDs and a positive level.")

    for professor_id, professor in problem.professors.items():
        if professor_id != professor.id:
            raise ValueError("Professor dictionary keys must match professor IDs.")
        if not isinstance(professor.employment_type, EmploymentType):
            raise ValueError(f"Professor {professor.id!r} has an unknown employment type.")
        validate_slots(professor.available_slots, f"Professor {professor.id!r}")
        if (
            professor.employment_type is EmploymentType.PART_TIME
            and (
                professor.weekly_target_periods is None
                or professor.weekly_target_periods < 0
            )
        ):
            raise ValueError(
                f"Part-time professor {professor.id!r} needs a nonnegative weekly target."
            )
        if (
            professor.employment_type is not EmploymentType.PART_TIME
            and professor.weekly_target_periods is not None
        ):
            raise ValueError(
                f"Only part-time professor targets are modeled for {professor.id!r}."
            )
        if any(
            day < 0 or day >= len(problem.day_names)
            for day in professor.authorized_workday_exception_days
        ):
            raise ValueError(f"Professor {professor.id!r} has an invalid workday exception.")

    for section_id, section in problem.sections.items():
        if section_id != section.id:
            raise ValueError("Section dictionary keys must match section IDs.")
        if section.program_id not in problem.programs:
            raise ValueError(f"Section {section.id!r} references an unknown program.")
        if section.year_level_id not in problem.year_levels:
            raise ValueError(f"Section {section.id!r} references an unknown year level.")
        validate_slots(section.available_slots, f"Section {section.id!r}")

    for subject_id, subject in problem.subjects.items():
        if subject_id != subject.id:
            raise ValueError("Subject dictionary keys must match subject IDs.")
        if not 1 <= subject.duration_periods <= problem.periods_per_day:
            raise ValueError(f"Subject {subject.id!r} has an invalid meeting duration.")

    for room_id, room in problem.rooms.items():
        if room_id != room.id:
            raise ValueError("Room dictionary keys must match room IDs.")
        if room.capacity < 1 or not room.room_type:
            raise ValueError(f"Room {room.id!r} needs a positive capacity and room type.")

    for offering_id, offering in problem.offerings.items():
        if offering_id != offering.id:
            raise ValueError("Offering dictionary keys must match offering IDs.")
        if offering.section_id not in problem.sections:
            raise ValueError(f"Offering {offering.id!r} references an unknown section.")
        if offering.subject_id not in problem.subjects:
            raise ValueError(f"Offering {offering.id!r} references an unknown subject.")
        if offering.meetings_per_week < 1:
            raise ValueError(f"Offering {offering.id!r} needs at least one weekly meeting.")
        if offering.expected_enrollment < 0 or not offering.required_room_type:
            raise ValueError(f"Offering {offering.id!r} has invalid room requirements.")

    assignment_keys: set[tuple[str, str]] = set()
    for assignment in problem.professor_assignments:
        if assignment.offering_id not in problem.offerings:
            raise ValueError("A professor assignment references an unknown offering.")
        if assignment.professor_id not in problem.professors:
            raise ValueError("A professor assignment references an unknown professor.")
        key = (assignment.offering_id, assignment.professor_id)
        if key in assignment_keys:
            raise ValueError("Professor assignments must be unique per offering.")
        assignment_keys.add(key)

    combined_ids: set[str] = set()
    combined_offerings: set[str] = set()
    for combined in problem.combined_classes:
        if combined.id in combined_ids:
            raise ValueError("Combined class IDs must be unique.")
        combined_ids.add(combined.id)
        if len(combined.offering_ids) < 2:
            raise ValueError("A combined class must explicitly include at least two offerings.")
        if len(set(combined.offering_ids)) != len(combined.offering_ids):
            raise ValueError(f"Combined class {combined.id!r} repeats an offering.")
        if any(offering_id not in problem.offerings for offering_id in combined.offering_ids):
            raise ValueError(f"Combined class {combined.id!r} references an unknown offering.")
        if combined_offerings.intersection(combined.offering_ids):
            raise ValueError("An offering cannot belong to multiple combined classes.")
        combined_offerings.update(combined.offering_ids)

        offerings = [problem.offerings[offering_id] for offering_id in combined.offering_ids]
        first = offerings[0]
        if len({offering.section_id for offering in offerings}) != len(offerings):
            raise ValueError(f"Combined class {combined.id!r} must use distinct sections.")
        if any(
            offering.subject_id != first.subject_id
            or offering.meetings_per_week != first.meetings_per_week
            or offering.required_room_type != first.required_room_type
            for offering in offerings[1:]
        ):
            raise ValueError(
                f"Combined class {combined.id!r} offerings must share subject, "
                "frequency, and room type."
            )


def build_required_meetings(
    problem: AcademicSchedulingProblem,
) -> tuple[RequiredClassMeeting, ...]:
    _validate_problem(problem)

    approved_by_offering: dict[str, set[str]] = {
        offering_id: set() for offering_id in problem.offerings
    }
    for assignment in problem.professor_assignments:
        if assignment.approved:
            approved_by_offering[assignment.offering_id].add(assignment.professor_id)

    grouped_offering_ids = {
        offering_id
        for combined in problem.combined_classes
        for offering_id in combined.offering_ids
    }
    meetings: list[RequiredClassMeeting] = []

    for combined in sorted(problem.combined_classes, key=lambda item: item.id):
        offering_ids = tuple(sorted(combined.offering_ids))
        offerings = [problem.offerings[offering_id] for offering_id in offering_ids]
        approved_professors = set.intersection(
            *(approved_by_offering[offering_id] for offering_id in offering_ids)
        )
        first = offerings[0]
        subject = problem.subjects[first.subject_id]
        section_ids = tuple(sorted(offering.section_id for offering in offerings))

        for meeting_number in range(1, first.meetings_per_week + 1):
            meetings.append(
                RequiredClassMeeting(
                    id=f"combined:{combined.id}:{meeting_number}",
                    offering_ids=offering_ids,
                    section_ids=section_ids,
                    subject_id=subject.id,
                    duration_periods=subject.duration_periods,
                    expected_enrollment=sum(
                        offering.expected_enrollment for offering in offerings
                    ),
                    required_room_type=first.required_room_type,
                    eligible_professor_ids=tuple(sorted(approved_professors)),
                )
            )

    for offering in sorted(problem.offerings.values(), key=lambda item: item.id):
        if offering.id in grouped_offering_ids:
            continue
        subject = problem.subjects[offering.subject_id]
        for meeting_number in range(1, offering.meetings_per_week + 1):
            meetings.append(
                RequiredClassMeeting(
                    id=f"offering:{offering.id}:{meeting_number}",
                    offering_ids=(offering.id,),
                    section_ids=(offering.section_id,),
                    subject_id=subject.id,
                    duration_periods=subject.duration_periods,
                    expected_enrollment=offering.expected_enrollment,
                    required_room_type=offering.required_room_type,
                    eligible_professor_ids=tuple(
                        sorted(approved_by_offering[offering.id])
                    ),
                )
            )

    return tuple(sorted(meetings, key=lambda item: item.id))
