"""Agent container entrypoint: block until the pod is stopped (spec 5.2, 5.5).

It has no security role. The egress guard init container has already gated the pod; the
Session Runner execs `agent_runtime.step` into this container for each boundary.
"""

import signal
import sys


def main() -> None:
    # As PID 1 the default SIGTERM disposition is ignored, so exit explicitly.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
