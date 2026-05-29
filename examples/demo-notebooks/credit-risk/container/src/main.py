"""SageMaker BYOC entry point: dispatch the `train` / `serve` command SageMaker passes."""

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
        raise SystemExit(f"unknown command: {cmd!r} (expected 'train' or 'serve')")
