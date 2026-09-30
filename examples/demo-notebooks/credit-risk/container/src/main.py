"""SageMaker BYOC entry point: dispatch the `train` / `serve` command SageMaker passes."""

import os
import sys

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "train":
        import train

        train.run()
    elif cmd == "serve":
        import serve

        serve.run()
    else:
        # anything else is a command to run in the image (SageMaker local mode
        # runs `chmod -R 777 <dir>` here on Linux to clean up root-owned output)
        os.execvp(cmd, sys.argv[1:])
