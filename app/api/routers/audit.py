"""Upload audit API and websocket."""

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.infrastructure.audit.upload_audit import audit_store

router = APIRouter()

@router.get("/audit/uploads", tags=["Audit"])
async def list_upload_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return recent B/L upload audit records."""
    return {
        "success": True,
        "data": audit_store.list_uploads(limit=limit, offset=offset),
    }


@router.get("/audit/uploads/{audit_id}", tags=["Audit"])
async def get_upload_audit(audit_id: str):
    """Return one audit record including the stored response payload."""
    item = audit_store.get_upload(audit_id, include_response=True)
    if not item:
        raise HTTPException(status_code=404, detail="Upload log not found")
    return {"success": True, "data": item}


@router.get("/audit/uploads/{audit_id}/file", tags=["Audit"])
async def download_audit_file(audit_id: str):
    """Download the original uploaded document saved for this audit record."""
    item = audit_store.get_upload(audit_id, include_response=False)
    if not item or not item.get("saved_path"):
        raise HTTPException(status_code=404, detail="Upload file not found")
    path = item["saved_path"]
    return FileResponse(
        path,
        filename=item.get("original_filename") or item.get("saved_filename") or "upload.bin",
        media_type=item.get("content_type") or "application/octet-stream",
    )


@router.websocket("/audit/ws")
async def upload_audit_socket(websocket: WebSocket):
    """Realtime stream of B/L upload audit changes."""
    await audit_store.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        audit_store.disconnect(websocket)
