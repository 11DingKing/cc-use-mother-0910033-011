"""启动开发用 API 服务。

用法：python3 tools/run_server.py [--host 127.0.0.1] [--port 8000] [--state var/state.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from practice_conflict.api import make_server
from practice_conflict.models import Actor, Role
from practice_conflict.service import PracticeConflictService
from practice_conflict.store import Store

# 开发演示令牌，生产环境应由部署方替换为真实身份源。
DEV_TOKENS = {
    "regulator-token": Actor("reg-1", Role.REGULATOR),
    "reviewer-token": Actor("rev-1", Role.REVIEWER),
    "officer-a-token": Actor("off-a", Role.OFFICER, institution_id="INST-A"),
    "officer-b-token": Actor("off-b", Role.OFFICER, institution_id="INST-B"),
    "practitioner-p1-token": Actor("p1-user", Role.PRACTITIONER, practitioner_id="P1"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="多机构执业冲突后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state", help="状态快照文件路径，用于重启后恢复数据")
    parser.add_argument("--default-buffer", type=int, default=30, help="未登记站点对的默认移动缓冲分钟数")
    args = parser.parse_args()

    store = Store()
    state_path = Path(args.state) if args.state else None
    if state_path and state_path.exists():
        store = Store.restore(json.loads(state_path.read_text(encoding="utf-8")))
        print(f"已从 {state_path} 恢复状态")

    service = PracticeConflictService(store, default_buffer_minutes=args.default_buffer)

    def persist() -> None:
        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(store.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")

    server = make_server(service, DEV_TOKENS, host=args.host, port=args.port, on_change=persist)
    print(f"服务已启动：http://{args.host}:{args.port}")
    print("演示令牌：" + "、".join(sorted(DEV_TOKENS)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        persist()
        server.server_close()


if __name__ == "__main__":
    main()
