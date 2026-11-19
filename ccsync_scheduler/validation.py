from __future__ import annotations

from collections import Counter

from ccsync_scheduler.models import (
    AcademicAssignment,
    AcademicSchedulingProblem,
    ScheduledSession,
    SchedulingProblem,
    TimeSlot,
)
from ccsync_scheduler.academic import allowed_workdays, build_required_meetings


def validate_assignments(
    problem: SchedulingProblem,
    assignments: tuple[ScheduledSession, ...],
) -> list[str]:
    errors: list[str] = []
    sessions = {session.id: session for session in problem.required_sessions}
    counts = Counter(assignment.session_id for assignment in assignments)

    for session_id in sessions:
        if counts[session_id] == 0:
            errors.append(f"Required session {session_id!r} was not scheduled.")
        elif counts[session_id] > 1:
            errors.append(f"Required session {session_id!r} was scheduled more than once.")

    for assignment in assignments:
        session = sessions.get(assignment.session_id)
        if session is None:
            errors.append(f"Unknown session {assignment.session_id!r} was scheduled.")
            continue

        subject = problem.subjects[session.subject_id]
        professor = problem.professors[session.professor_id]
        section = problem.sections[session.section_id]
        start = assignment.start

        if not (0 <= start.day < problem.days):
            errors.append(f"Session {session.id!r} has an invalid day.")
            continue
        if not (0 <= start.period < problem.periods_per_day):
            errors.append(f"Session {session.id!r} has an invalid start period.")
            continue
        if start.period + subject.duration_periods > problem.periods_per_day:
            errors.append(f"Session {session.id!r} crosses a day boundary.")
            continue

        occupied = {
            TimeSlot(start.day, period)
            for period in range(start.period, start.period + subject.duration_periods)
        }
        if not occupied.issubset(problem.available_slots):
            errors.append(f"Session {session.id!r} uses an unavailable time slot.")
        if not occupied.issubset(professor.available_slots):
            errors.append(f"Session {session.id!r} is outside professor availability.")
        if not occupied.issubset(section.available_slots):
            errors.append(f"Session {session.id!r} is outside section availability.")
        if assignment.room_id not in problem.rooms:
            errors.append(f"Session {session.id!r} uses an unknown room.")
        elif assignment.room_id not in session.eligible_room_ids:
            errors.append(f"Session {session.id!r} uses an ineligible room.")

    valid_assignments = [
        (assignment, sessions[assignment.session_id])
        for assignment in assignments
        if assignment.session_id in sessions
    ]
    for index, (first, first_session) in enumerate(valid_assignments):
        first_subject = problem.subjects[first_session.subject_id]
        first_start = first.start.day * problem.periods_per_day + first.start.period
        first_end = first_start + first_subject.duration_periods

        for second, second_session in valid_assignments[index + 1 :]:
            second_subject = problem.subjects[second_session.subject_id]
            second_start = second.start.day * problem.periods_per_day + second.start.period
            second_end = second_start + second_subject.duration_periods
            overlaps = first_start < second_end and second_start < first_end
            if not overlaps:
                continue

            if first_session.professor_id == second_session.professor_id:
                errors.append(
                    f"Professor {first_session.professor_id!r} has overlapping sessions."
                )
            if first_session.section_id == second_session.section_id:
                errors.append(f"Section {first_session.section_id!r} has overlapping sessions.")
            if first.room_id == second.room_id:
                errors.append(f"Room {first.room_id!r} has overlapping sessions.")

    return errors


def validate_academic_assignments(
    problem: AcademicSchedulingProblem,
    assignments: tuple[AcademicAssignment, ...],
) -> list[str]:
    meetings = build_required_meetings(problem)
    meeting_by_id = {meeting.id: meeting for meeting in meetings}
    counts = Counter(assignment.meeting_id for assignment in assignments)
    errors: list[str] = []

    for meeting in meetings:
        if counts[meeting.id] == 0:
            errors.append(f"Required meeting {meeting.id!r} was not scheduled.")
        elif counts[meeting.id] > 1:
            errors.append(f"Required meeting {meeting.id!r} was scheduled more than once.")

    for assignment in assignments:
        meeting = meeting_by_id.get(assignment.meeting_id)
        if meeting is None:
            errors.append(f"Unknown meeting {assignment.meeting_id!r} was scheduled.")
            continue

        if assignment.professor_id not in meeting.eligible_professor_ids:
            errors.append(
                f"Meeting {meeting.id!r} has no approved assignment for professor "
                f"{assignment.professor_id!r}."
            )
        professor = problem.professors.get(assignment.professor_id)
        room = problem.rooms.get(assignment.room_id)
        start = assignment.start

        if professor is None:
            errors.append(f"Meeting {meeting.id!r} uses an unknown professor.")
        if room is None:
            errors.append(f"Meeting {meeting.id!r} uses an unknown room.")
        if not (0 <= start.day < len(problem.day_names)):
            errors.append(f"Meeting {meeting.id!r} has an invalid day.")
            continue
        if not (0 <= start.period < problem.periods_per_day):
            errors.append(f"Meeting {meeting.id!r} has an invalid start period.")
            continue
        if start.period + meeting.duration_periods > problem.periods_per_day:
            errors.append(f"Meeting {meeting.id!r} crosses a day boundary.")
            continue

        occupied = {
            TimeSlot(start.day, period)
            for period in range(start.period, start.period + meeting.duration_periods)
        }
        if not occupied.issubset(problem.available_slots):
            errors.append(f"Meeting {meeting.id!r} uses globally unavailable time.")
        if professor is not None:
            if not occupied.issubset(professor.available_slots):
                errors.append(f"Meeting {meeting.id!r} is outside professor availability.")
            if start.day not in allowed_workdays(problem, assignment.professor_id):
                errors.append(f"Meeting {meeting.id!r} violates professor workday rules.")
        for section_id in meeting.section_ids:
            section = problem.sections[section_id]
            if not occupied.issubset(section.available_slots):
                errors.append(
                    f"Meeting {meeting.id!r} is outside section {section_id!r} availability."
                )

        if room is not None:
            if room.capacity < meeting.expected_enrollment:
                errors.append(f"Meeting {meeting.id!r} exceeds room capacity.")
            if room.room_type != meeting.required_room_type:
                errors.append(f"Meeting {meeting.id!r} uses an invalid room type.")

    valid_assignments = [
        (assignment, meeting_by_id[assignment.meeting_id])
        for assignment in assignments
        if assignment.meeting_id in meeting_by_id
    ]
    for index, (first, first_meeting) in enumerate(valid_assignments):
        for second, second_meeting in valid_assignments[index + 1 :]:
            if first.start.day != second.start.day:
                continue
            first_end = first.start.period + first_meeting.duration_periods
            second_end = second.start.period + second_meeting.duration_periods
            overlaps = (
                first.start.period < second_end
                and second.start.period < first_end
            )
            if not overlaps:
                continue

            if first.professor_id == second.professor_id:
                errors.append(
                    f"Professor {first.professor_id!r} has overlapping meetings."
                )
            if set(first_meeting.section_ids).intersection(second_meeting.section_ids):
                errors.append("A section has overlapping meetings.")
            if first.room_id == second.room_id:
                errors.append(f"Room {first.room_id!r} has overlapping meetings.")

    return errors
