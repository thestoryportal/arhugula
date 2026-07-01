"""Enable ``python -m harness_runtime.cli`` invocation.

Authority: C-RT-29 HarnessRunCLI entrypoint surface.
"""

from harness_runtime.cli.app import main

if __name__ == "__main__":
    main()
