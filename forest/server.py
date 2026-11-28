"""服务入口：python -m forest.server --host 127.0.0.1 --port 8080 --db forest.sqlite3"""
from __future__ import annotations

import argparse
import json

from .http_app import build_server


def main():
    parser = argparse.ArgumentParser(description="县域造林合同后端（合成数据）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="forest-state.sqlite3")
    parser.add_argument("--fund-base-url", default="http://127.0.0.1:8765",
                        help="合成资金协作方回放地址；留空则派发时返回 502")
    parser.add_argument("--no-seed", action="store_true", help="不装载 catalog 合成资料")
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args()
    server = build_server(args.db, args.fund_base_url, args.host, args.port,
                          access_log=args.access_log, do_seed=not args.no_seed)
    print(json.dumps({"host": args.host, "port": server.server_address[1], "db": args.db,
                      "fund_base_url": args.fund_base_url}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
