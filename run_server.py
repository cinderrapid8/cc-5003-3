#!/usr/bin/env python3
"""Start the pickup re-recording workbench web service."""

import argparse
import os

from workbench.server import WorkbenchServer
from workbench.store import Store


def main():
    ap = argparse.ArgumentParser(description="有声书补录选段工作台")
    ap.add_argument("--data", default="data", help="工程数据目录")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "workbench", "static")
    store = Store(args.data)
    server = WorkbenchServer((args.host, args.port), store, static_dir)
    print("workbench on http://%s:%d  (data=%s)"
          % (args.host, args.port, store.data_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
