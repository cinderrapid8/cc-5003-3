import argparse
import os

from .service import build_server


def main():
    ap = argparse.ArgumentParser(description="Audiobook re-recording workbench")
    ap.add_argument("--data", default=os.environ.get("STUDIO_DATA", "./data"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    httpd = build_server(os.path.abspath(args.data), args.host, args.port)
    print(f"workbench listening on http://{args.host}:{httpd.server_address[1]}  data={args.data}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
