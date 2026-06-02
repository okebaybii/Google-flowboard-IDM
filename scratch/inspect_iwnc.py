import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pathlib import Path
sys.path.append(r"c:\Users\Admin\Documents\Google-flowboard-IDM\agent")

from flowboard.db import get_session
from flowboard.db.models import Node
from sqlmodel import select

with get_session() as session:
    nodes = session.exec(select(Node)).all()
    print(f"Total nodes: {len(nodes)}")
    found = False
    for n in nodes:
        sid = str(getattr(n, "short_id", None) or "")
        if sid.lower() == "iwnc":
            found = True
            print(f"FOUND! Node ID: {n.id} | short_id: {n.short_id} | Type: {n.type} | Status: {n.status} | Data: {n.data}")
            
            # Print upstream nodes details
            from flowboard.db.models import Edge
            edges = session.exec(select(Edge).where(Edge.target_id == n.id)).all()
            for e in edges:
                src = session.get(Node, e.source_id)
                if src:
                    print(f"  Upstream Node ID: {src.id} ({src.short_id})")
                    print(f"    Type: {src.type}")
                    print(f"    Status: {src.status}")
                    print(f"    AspectRatio: {src.data.get('aspectRatio')}")
                    print(f"    MediaId: {src.data.get('mediaId')}")
            break
    if not found:
        print("Not found by short_id column.")
        for n in nodes[:5]:
            print(f"ID: {n.id} | short_id: {n.short_id}")
