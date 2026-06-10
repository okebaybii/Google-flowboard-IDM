"""Social Block routes for managing social media posting blocks.

Handles CRUD operations for Social Blocks and scheduling posts.
"""
import logging
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import APIRouter, HTTPException, Query, Depends
from sqlmodel import select, Session

from flowboard.db import get_session
from flowboard.db.models import SocialBlock, SocialBlockPost, Node, SocialAccount, Edge, Asset
from flowboard.services.platform_poster import get_platform_poster
from pydantic import BaseModel

class PostNowRequest(BaseModel):
    platforms: List[str]
    content: str

class ScheduleRequest(BaseModel):
    platforms: List[str]
    content: str
    scheduled_time: datetime
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None

logger = logging.getLogger(__name__)

def get_connected_asset_paths(node_id: int, session: Session) -> List[Dict[str, str]]:
    import os
    # 1. Find all edges connected to this node
    edges = session.exec(
        select(Edge).where(
            (Edge.source_id == node_id) | (Edge.target_id == node_id)
        )
    ).all()
    
    connected_node_ids = set()
    edge_by_source = {}
    for edge in edges:
        if edge.source_id != node_id:
            connected_node_ids.add(edge.source_id)
            edge_by_source[edge.source_id] = edge
        if edge.target_id != node_id:
            connected_node_ids.add(edge.target_id)
            
    if not connected_node_ids:
        return []
        
    connected_nodes = session.exec(
        select(Node).where(Node.id.in_(list(connected_node_ids)))
    ).all()
    
    media_ids = []
    for cnode in connected_nodes:
        edge = edge_by_source.get(cnode.id)
        data = cnode.data or {}
        
        pinned_mid = None
        if edge and edge.source_variant_idx is not None:
            mids = data.get("mediaIds")
            if isinstance(mids, list) and 0 <= edge.source_variant_idx < len(mids):
                pinned_mid = mids[edge.source_variant_idx]
                
        if pinned_mid:
            media_ids.append(pinned_mid)
        else:
            mid = data.get("mediaId")
            if isinstance(mid, str) and mid:
                media_ids.append(mid)
            mids = data.get("mediaIds")
            if isinstance(mids, list):
                for m in mids:
                    if isinstance(m, str) and m:
                        media_ids.append(m)
                        
    # Get all assets matching connected node IDs or uuid_media_ids
    from sqlalchemy import or_
    conditions = [Asset.node_id.in_(list(connected_node_ids))]
    if media_ids:
        conditions.append(Asset.uuid_media_id.in_(media_ids))
        
    assets = session.exec(
        select(Asset).where(or_(*conditions))
    ).all()
    
    # Filter for local paths that exist, preserving relevance order
    items = []
    seen_paths = set()
    asset_by_uuid = {asset.uuid_media_id: asset for asset in assets if asset.uuid_media_id}
    asset_by_node = {asset.node_id: asset for asset in assets if asset.node_id}
    
    def add_item(asset):
        if asset and asset.local_path and os.path.exists(asset.local_path):
            if asset.local_path not in seen_paths:
                seen_paths.add(asset.local_path)
                items.append({
                    "path": asset.local_path,
                    "kind": asset.kind or "image"
                })
                
    for mid in media_ids:
        add_item(asset_by_uuid.get(mid))
                
    for cid in connected_node_ids:
        add_item(asset_by_node.get(cid))
                
    for asset in assets:
        add_item(asset)
                
    return items

router = APIRouter(prefix="/api/social-blocks", tags=["social-blocks"])


def _session_dependency():
    with get_session() as session:
        yield session


# ============================================================================
# CREATE
# ============================================================================

@router.post("")
def create_social_block(
    node_id: int,
    board_id: int,
    platforms: List[str] = None,
    content: str = "",
    content_type: str = "manual",
    session: Session = Depends(_session_dependency),
):
    """Create a new Social Block."""
    try:
        # Verify node exists
        node = session.exec(select(Node).where(Node.id == node_id)).first()
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        
        # Create Social Block
        social_block = SocialBlock(
            node_id=node_id,
            board_id=board_id,
            platforms=str(platforms or []),
            content=content,
            content_type=content_type,
            status="draft",
        )
        session.add(social_block)
        session.commit()
        session.refresh(social_block)
        
        return {
            "id": social_block.id,
            "node_id": social_block.node_id,
            "platforms": platforms or [],
            "content": social_block.content,
            "status": social_block.status,
        }
    except Exception as e:
        logger.error(f"Error creating social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# READ
# ============================================================================

@router.get("/{block_id}")
def get_social_block(block_id: int, session: Session = Depends(_session_dependency)):
    """Get a Social Block by ID."""
    
    try:
        block = session.exec(
            select(SocialBlock).where(SocialBlock.id == block_id)
        ).first()
        
        if not block:
            raise HTTPException(status_code=404, detail="Social Block not found")
        
        return {
            "id": block.id,
            "node_id": block.node_id,
            "platforms": block.platforms,
            "content": block.content,
            "content_type": block.content_type,
            "status": block.status,
            "scheduled_time": block.scheduled_time,
            "created_at": block.created_at,
        }
    except Exception as e:
        logger.error(f"Error getting social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def list_social_blocks(
    board_id: int = Query(None),
    status: str = Query(None),
    session: Session = Depends(_session_dependency),
):
    """List Social Blocks with optional filtering."""
    
    try:
        query = select(SocialBlock)
        
        if board_id:
            query = query.where(SocialBlock.board_id == board_id)
        if status:
            query = query.where(SocialBlock.status == status)
        
        blocks = session.exec(query).all()
        
        return [
            {
                "id": block.id,
                "node_id": block.node_id,
                "platforms": block.platforms,
                "content": block.content[:100] + "..." if len(block.content) > 100 else block.content,
                "status": block.status,
                "created_at": block.created_at,
            }
            for block in blocks
        ]
    except Exception as e:
        logger.error(f"Error listing social blocks: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# UPDATE
# ============================================================================

@router.put("/{block_id}")
def update_social_block(
    block_id: int,
    platforms: List[str] = None,
    content: str = None,
    content_type: str = None,
    ai_prompt: str = None,
    session: Session = Depends(_session_dependency),
):
    """Update a Social Block."""
    
    try:
        block = session.exec(
            select(SocialBlock).where(SocialBlock.id == block_id)
        ).first()
        
        if not block:
            raise HTTPException(status_code=404, detail="Social Block not found")
        
        # Update fields
        if platforms is not None:
            block.platforms = str(platforms)
        if content is not None:
            block.content = content
        if content_type is not None:
            block.content_type = content_type
        if ai_prompt is not None:
            block.ai_prompt = ai_prompt
        
        block.updated_at = datetime.utcnow()
        
        session.add(block)
        session.commit()
        session.refresh(block)
        
        return {
            "id": block.id,
            "platforms": platforms or [],
            "content": block.content,
            "status": block.status,
            "updated_at": block.updated_at,
        }
    except Exception as e:
        logger.error(f"Error updating social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SCHEDULE
# ============================================================================

@router.post("/node/{node_id}/schedule")
def schedule_social_block(
    node_id: int,
    req: ScheduleRequest,
):
    """Schedule a Social Block to post to platforms (creates block if not exists)."""
    
    platforms = req.platforms
    content = req.content
    scheduled_time = req.scheduled_time
    is_recurring = req.is_recurring
    recurrence_pattern = req.recurrence_pattern
    
    try:
        with get_session() as session:
            # 1. Verify Node exists
            node = session.exec(select(Node).where(Node.id == node_id)).first()
            if not node:
                raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
                
            board_id = node.board_id
            
            # 2. Get or create SocialBlock
            block = session.exec(
                select(SocialBlock).where(SocialBlock.node_id == node_id)
            ).first()
            
            if not block:
                block = SocialBlock(
                    node_id=node_id,
                    board_id=board_id,
                    platforms=str(platforms),
                    content=content,
                    status="draft",
                )
                session.add(block)
                session.commit()
                session.refresh(block)
            
            # Validate scheduled time is in future
            now = datetime.now(scheduled_time.tzinfo) if scheduled_time.tzinfo else datetime.utcnow()
            if scheduled_time <= now:
                raise HTTPException(status_code=400, detail="Scheduled time must be in the future")
            
            # Update block
            block.platforms = str(platforms)
            block.content = content
            block.scheduled_time = scheduled_time
            block.is_recurring = is_recurring
            block.recurrence_pattern = recurrence_pattern
            block.status = "scheduled"
            block.updated_at = datetime.utcnow()
            
            session.add(block)
            session.commit()
            
            # Create SocialBlockPost entries for each platform
            for platform in platforms:
                post = SocialBlockPost(
                    social_block_id=block.id,
                    platform=platform,
                    content=block.content,
                    scheduled_time=scheduled_time,
                    status="pending",
                )
                session.add(post)
            
            session.commit()
            
            return {
                "id": block.id,
                "status": "scheduled",
                "platforms": platforms,
                "scheduled_time": scheduled_time,
                "posts_created": len(platforms),
            }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error scheduling social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# POST TO PLATFORMS
# ============================================================================

@router.post("/{block_id}/post")
async def post_social_block(block_id: int, session: Session = Depends(_session_dependency)):
    """Post a Social Block to all scheduled platforms."""
    import os
    
    try:
        block = session.exec(
            select(SocialBlock).where(SocialBlock.id == block_id)
        ).first()
        
        if not block:
            raise HTTPException(status_code=404, detail="Social Block not found")
        
        # Get all pending posts for this block
        posts = session.exec(
            select(SocialBlockPost).where(
                (SocialBlockPost.social_block_id == block_id) &
                (SocialBlockPost.status == "pending")
            )
        ).all()
        
        if not posts:
            raise HTTPException(status_code=400, detail="No pending posts found")
        
        # Get platform poster
        poster = get_platform_poster()
        results = []
        
        # Post to each platform
        for post in posts:
            try:
                token = None
                page_id = None
                business_account_id = None

                # Priority 1: Check .env for Facebook
                if post.platform == "facebook" and os.getenv("FB_PAGE__ACCESS_TOKEN"):
                    page_id = os.getenv("FB_PAGE__ID")
                    token = os.getenv("FB_PAGE__ACCESS_TOKEN")

                # Priority 2: Query database SocialAccount
                if not token:
                    account = session.exec(
                        select(SocialAccount).where(
                            SocialAccount.platform == post.platform
                        )
                    ).first()
                    if account:
                        token = account.access_token
                        page_id = account.account_id if post.platform == "facebook" else None
                        business_account_id = account.account_id if post.platform == "instagram" else None
                
                if not token:
                    post.status = "failed"
                    post.error_message = f"No credentials configured for {post.platform}"
                    session.add(post)
                    results.append({
                        "platform": post.platform,
                        "status": "failed",
                        "error": "No credentials configured",
                    })
                    continue
                
                # Get connected media items
                media_items = get_connected_asset_paths(block.node_id, session)
                image_path = media_items[0]["path"] if media_items else None

                # Post to platform
                post_result = await poster.post_to_platform(
                    platform=post.platform,
                    content=post.content,
                    token=token,
                    page_id=page_id,
                    business_account_id=business_account_id,
                    image_path=image_path,
                    media_items=media_items,
                )
                
                # If successful, save Facebook post ID
                if post.platform == "facebook" and post_result.get("status") == "success":
                    block.facebook_post_id = post_result.get("post_id")
                    session.add(block)
                
                # Update post status
                if post_result["status"] == "success":
                    post.status = "posted"
                    post.posted_url = post_result.get("url")
                    post.posted_at = datetime.utcnow()
                else:
                    post.status = "failed"
                    post.error_message = post_result.get("error", "Unknown error")
                
                session.add(post)
                results.append(post_result)
                
            except Exception as e:
                logger.error(f"Error posting to {post.platform}: {str(e)}")
                post.status = "failed"
                post.error_message = str(e)
                session.add(post)
                results.append({
                    "platform": post.platform,
                    "status": "failed",
                    "error": str(e),
                })
        
        # Update block status
        block.status = "posted"
        session.add(block)
        session.commit()
        
        return {
            "id": block.id,
            "status": "posted",
            "results": results,
        }
    except Exception as e:
        logger.error(f"Error posting social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/node/{node_id}/post-now")
async def post_social_block_now(
    node_id: int,
    req: PostNowRequest,
):
    """Post a Social Block immediately by Node ID (creates block if not exists)."""
    import os
    
    platforms = req.platforms
    content = req.content
    
    try:
        with get_session() as session:
            # 1. Verify Node exists
            node = session.exec(select(Node).where(Node.id == node_id)).first()
            if not node:
                raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
                
            board_id = node.board_id
            
            # 2. Get or create SocialBlock
            block = session.exec(
                select(SocialBlock).where(SocialBlock.node_id == node_id)
            ).first()
            
            if not block:
                block = SocialBlock(
                    node_id=node_id,
                    board_id=board_id,
                    platforms=str(platforms),
                    content=content,
                    status="draft",
                )
                session.add(block)
                session.commit()
                session.refresh(block)
            else:
                block.platforms = str(platforms)
                block.content = content
                block.updated_at = datetime.utcnow()
                session.add(block)
                session.commit()
                session.refresh(block)
            
            poster = get_platform_poster()
            results = []
            
            for platform in platforms:
                try:
                    token = None
                    page_id = None
                    business_account_id = None

                    # Priority 1: Check .env for Facebook
                    if platform == "facebook" and os.getenv("FB_PAGE__ACCESS_TOKEN"):
                        page_id = os.getenv("FB_PAGE__ID")
                        token = os.getenv("FB_PAGE__ACCESS_TOKEN")

                    # Priority 2: Query database SocialAccount
                    if not token:
                        account = session.exec(
                            select(SocialAccount).where(
                                SocialAccount.platform == platform
                            )
                        ).first()
                        if account:
                            token = account.access_token
                            page_id = account.account_id if platform == "facebook" else None
                            business_account_id = account.account_id if platform == "instagram" else None
                    
                    if not token:
                        results.append({
                            "platform": platform,
                            "status": "failed",
                            "error": f"No credentials configured for {platform}",
                        })
                        continue
                    
                    # Get connected media items
                    media_items = get_connected_asset_paths(node_id, session)
                    image_path = media_items[0]["path"] if media_items else None

                    post_result = await poster.post_to_platform(
                        platform=platform,
                        content=content,
                        token=token,
                        page_id=page_id,
                        business_account_id=business_account_id,
                        image_path=image_path,
                        media_items=media_items,
                    )
                    
                    # If successful, save Facebook post ID
                    if platform == "facebook" and post_result.get("status") == "success":
                        block.facebook_post_id = post_result.get("post_id")
                        session.add(block)
                    
                    results.append(post_result)
                    
                except Exception as e:
                    logger.error(f"Error posting to {platform}: {str(e)}")
                    results.append({
                        "platform": platform,
                        "status": "failed",
                        "error": str(e),
                    })
            
            # Create SocialBlockPost log entries for history
            for res in results:
                post = SocialBlockPost(
                    social_block_id=block.id,
                    platform=res["platform"],
                    content=content,
                    status="posted" if res.get("status") == "success" else "failed",
                    error_message=res.get("error") if res.get("status") != "success" else None,
                    posted_url=res.get("url"),
                    posted_at=datetime.utcnow() if res.get("status") == "success" else None,
                    scheduled_time=datetime.utcnow(),
                )
                session.add(post)
            
            # Check if all succeeded or partial
            failed_count = sum(1 for res in results if res.get("status") != "success")
            if failed_count == 0:
                block.status = "posted"
            elif failed_count < len(platforms):
                block.status = "partial"
            else:
                block.status = "failed"
                
            session.add(block)
            session.commit()
            
            return {
                "id": block.id,
                "status": block.status,
                "results": results,
            }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Error in post-now: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{block_id}/status")
def get_social_block_status(block_id: int, session: Session = Depends(_session_dependency)):
    """Get status of a Social Block and its posts."""
    
    try:
        block = session.exec(
            select(SocialBlock).where(SocialBlock.id == block_id)
        ).first()
        
        if not block:
            raise HTTPException(status_code=404, detail="Social Block not found")
        
        # Get posts
        posts = session.exec(
            select(SocialBlockPost).where(SocialBlockPost.social_block_id == block_id)
        ).all()
        
        return {
            "id": block.id,
            "status": block.status,
            "scheduled_time": block.scheduled_time,
            "posts": [
                {
                    "id": post.id,
                    "platform": post.platform,
                    "status": post.status,
                    "posted_url": post.posted_url,
                    "error_message": post.error_message,
                    "posted_at": post.posted_at,
                }
                for post in posts
            ],
        }
    except Exception as e:
        logger.error(f"Error getting social block status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# DELETE
# ============================================================================

@router.delete("/{block_id}")
def delete_social_block(block_id: int, session: Session = Depends(_session_dependency)):
    """Delete a Social Block."""
    
    try:
        block = session.exec(
            select(SocialBlock).where(SocialBlock.id == block_id)
        ).first()
        
        if not block:
            raise HTTPException(status_code=404, detail="Social Block not found")
        
        # Delete associated posts
        posts = session.exec(
            select(SocialBlockPost).where(SocialBlockPost.social_block_id == block_id)
        ).all()
        
        for post in posts:
            session.delete(post)
        
        session.delete(block)
        session.commit()
        
        return {"message": "Social Block deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting social block: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
