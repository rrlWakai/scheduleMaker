from __future__ import annotations

import os

import uvicorn


def main() -> None:
    try:
        port = int(os.environ.get("SCHEDULER_PORT", "8000"))
    except ValueError as exc:
        raise SystemExit("SCHEDULER_PORT must be an integer from 1 to 65535.") from exc

    if not 1 <= port <= 65535:
        raise SystemExit("SCHEDULER_PORT must be an integer from 1 to 65535.")

    uvicorn.run(
        "ccsync_scheduler.main:app",
        host="127.0.0.1",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
