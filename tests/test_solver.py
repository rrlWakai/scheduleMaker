from dataclasses import replace

from ortools.sat.python import cp_model

from ccsync_scheduler.demo import make_feasible_problem, make_infeasible_problem
from ccsync_scheduler.models import SolverStatus, TimeSlot
from ccsync_scheduler.solver import _status_for_code, solve
from ccsync_scheduler.validation import validate_assignments


def test_python_and_ortools_modules_import() -> None:
    from ccsync_scheduler import demo, main, models, solver, validation

    assert all((demo, main, models, solver, validation, cp_model))


def test_ortools_can_solve_a_small_model() -> None:
    model = cp_model.CpModel()
    variable = model.NewBoolVar("available")
    model.Add(variable == 1)
    solver = cp_model.CpSolver()

    status = solver.Solve(model)

    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    assert solver.Value(variable) == 1


def test_feasible_proof_of_concept_is_hard_constraint_valid() -> None:
    problem = make_feasible_problem()

    result = solve(problem)

    assert result.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
    assert len(result.assignments) == len(problem.required_sessions)
    assert validate_assignments(problem, result.assignments) == []


def test_infeasible_problem_is_not_reported_as_success() -> None:
    result = solve(make_infeasible_problem())

    assert result.status is SolverStatus.INFEASIBLE
    assert result.assignments == ()


def test_solver_status_mapping_distinguishes_all_cp_sat_outcomes() -> None:
    assert _status_for_code(cp_model.OPTIMAL) is SolverStatus.OPTIMAL
    assert _status_for_code(cp_model.FEASIBLE) is SolverStatus.FEASIBLE
    assert _status_for_code(cp_model.INFEASIBLE) is SolverStatus.INFEASIBLE
    assert _status_for_code(cp_model.UNKNOWN) is SolverStatus.UNKNOWN
    assert _status_for_code(cp_model.MODEL_INVALID) is SolverStatus.MODEL_INVALID


def test_independent_validator_detects_duplicate_session() -> None:
    problem = make_feasible_problem()
    result = solve(problem)
    duplicate = result.assignments + (result.assignments[0],)

    errors = validate_assignments(problem, duplicate)

    assert any("more than once" in error for error in errors)


def test_independent_validator_detects_availability_and_room_violations() -> None:
    problem = make_feasible_problem()
    result = solve(problem)
    invalid_assignments = tuple(
        replace(
            assignment,
            start=TimeSlot(0, 0),
            room_id="room-lab",
        )
        if assignment.session_id == "session-networks"
        else assignment
        for assignment in result.assignments
    )

    errors = validate_assignments(problem, invalid_assignments)

    assert any("outside professor availability" in error for error in errors)
    assert any("ineligible room" in error for error in errors)
