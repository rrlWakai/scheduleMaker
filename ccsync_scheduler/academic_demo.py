from __future__ import annotations

import json

from ccsync_scheduler.academic import build_required_meetings
from ccsync_scheduler.academic_solver import solve_academic
from ccsync_scheduler.demo import make_global_problem


def main() -> None:
    problem = make_global_problem()
    result = solve_academic(problem)
    meetings = {meeting.id: meeting for meeting in build_required_meetings(problem)}
    output = {
        "status": result.status.value,
        "program_count": len(problem.programs),
        "year_level_count": len(problem.year_levels),
        "section_count": len(problem.sections),
        "offering_count": len(problem.offerings),
        "required_meeting_count": len(meetings),
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "optimization_metrics": (
            result.metrics.__dict__ if result.metrics is not None else None
        ),
        "assignments": [
            {
                "meeting_id": assignment.meeting_id,
                "offerings": meetings[assignment.meeting_id].offering_ids,
                "sections": meetings[assignment.meeting_id].section_ids,
                "professor_id": assignment.professor_id,
                "day": problem.day_names[assignment.start.day],
                "start_period": assignment.start.period,
                "duration_periods": meetings[
                    assignment.meeting_id
                ].duration_periods,
                "room_id": assignment.room_id,
            }
            for assignment in result.assignments
        ],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
