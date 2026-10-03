"""Shared windowed executable for Commander and its bundled Assistant."""

import sys


def main() -> int:
    from commander_gui.applog import install_excepthook

    install_excepthook()
    if "--packaging-smoke-test" in sys.argv:
        from commander_gui.packaging_smoke import run

        return run(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "--assistant":
        from assistant.__main__ import main as assistant_main

        sys.argv.pop(1)
        assistant_main()
        return 0
    from commander_gui.main import main as commander_main

    return commander_main()


if __name__ == "__main__":
    raise SystemExit(main())
