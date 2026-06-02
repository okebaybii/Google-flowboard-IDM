import sys
import io
# Set stdout to UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pathlib import Path
sys.path.append(r"c:\Users\Admin\Documents\Google-flowboard-IDM\agent")

from flowboard.db import get_session
from flowboard.db.models import Node, Request
from sqlmodel import select

with get_session() as session:
    # 1. Check video_assembly nodes
    nodes = session.exec(select(Node).where(Node.type == 'video_assembly')).all()
    print("=== VIDEO ASSEMBLY NODES ===")
    for n in nodes:
        print(f"Node ID: {n.id}")
        print(f"  Status: {n.status}")
        print(f"  Error: {n.data.get('error')}")
        print(f"  ErrorHint: {n.data.get('errorHint')}")
        print(f"  MediaId: {n.data.get('mediaId')}")
        
    # 2. Check other video nodes that might be errored
    v_nodes = session.exec(select(Node).where(Node.type == 'video')).all()
    print("\n=== UPSTREAM VIDEO NODES ===")
    for n in v_nodes:
        if n.status == "error" or n.data.get("error"):
            print(f"Node ID: {n.id} ({n.data.get('title')})")
            print(f"  Status: {n.status}")
            print(f"  Error: {n.data.get('error')}")
            print(f"  ErrorHint: {n.data.get('errorHint')}")

    # 3. Check latest Request logs
    reqs = session.exec(select(Request).order_by(Request.id.desc()).limit(15)).all()
    print("\n=== LATEST REQUESTS ===")
    for r in reqs:
        print(f"Req ID: {r.id} | Node ID: {r.node_id} | Type: {r.type} | Status: {r.status}")
        if r.status in ("failed", "error") or r.error:
            print(f"  Error: {r.error}")
