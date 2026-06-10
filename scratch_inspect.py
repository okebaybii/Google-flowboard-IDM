import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, 'agent')
from flowboard.db import get_session
from flowboard.db.models import Board, Node, Edge
from sqlmodel import select

with get_session() as s:
    boards = s.exec(select(Board)).all()
    for b in boards:
        if 'unitine' in b.name.lower() or '04' in b.name:
            print(f'Board ID: {b.id}, Name: {b.name}')
            nodes = s.exec(select(Node).where(Node.board_id == b.id)).all()
            for n in nodes:
                prompt_text = str(n.data.get("prompt", ""))[:150].replace('\n', ' ')
                print(f'Node {n.id} ({n.type}): {prompt_text}')
                if n.type == 'character':
                    print(f'  Character Media: {n.data.get("mediaId")}, source: {n.data.get("sourceImage")}')
