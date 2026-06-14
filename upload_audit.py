from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Request, UploadFile, WebSocket
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in {".", "-", "_"}:
            keep.append(ch)
        else:
            keep.append("_")
    value = "".join(keep).strip("._")
    return value[:140] or "upload.bin"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _truncate_json(value: Any, max_chars: int = 220_000) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars]
    return json.dumps(
        {
            "truncated": True,
            "max_chars": max_chars,
            "preview": trimmed,
        },
        ensure_ascii=False,
    )


def _response_summary(value: Any) -> Dict[str, Any]:
    data = _jsonable(value)
    if not isinstance(data, dict):
        return {"success": None, "message": "Non JSON response"}
    dataverse_result = data.get("dataverse_result") or {}
    result_count = None
    if isinstance(dataverse_result, dict):
        result_count = dataverse_result.get("count")
        if result_count is None and dataverse_result.get("master_id"):
            result_count = 1
    return {
        "success": data.get("success"),
        "error": data.get("error"),
        "dataverse_error": data.get("dataverse_error"),
        "operation_id": dataverse_result.get("master_id") if isinstance(dataverse_result, dict) else None,
        "result_count": result_count,
        "quality": data.get("extraction_quality") or {},
    }


class UploadAuditStore:
    def __init__(self) -> None:
        root = self._resolve_root()
        self.root = root
        self.files_dir = root / "files"
        self.db_path = root / "upload_audit.sqlite3"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._clients: Set[WebSocket] = set()
        self._init_db()

    def _resolve_root(self) -> Path:
        candidates = [
            Path(os.environ.get("BL_AUDIT_DIR", "upload_audit")),
            Path("/tmp/bl_upload_audit"),
        ]
        last_error: Optional[Exception] = None
        for candidate in candidates:
            try:
                root = candidate.resolve()
                (root / "files").mkdir(parents=True, exist_ok=True)
                return root
            except Exception as exc:
                last_error = exc
                logger.warning("Audit storage path %s is not writable: %s", candidate, exc)
        raise RuntimeError("No writable upload audit storage path is available") from last_error

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_audit (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    endpoint TEXT,
                    method TEXT,
                    uploader_name TEXT,
                    uploader_id TEXT,
                    uploader_email TEXT,
                    client_ip TEXT,
                    user_agent TEXT,
                    original_filename TEXT,
                    saved_filename TEXT,
                    saved_path TEXT,
                    content_type TEXT,
                    file_size INTEGER,
                    sha256 TEXT,
                    bl_type TEXT,
                    post_to_dataverse INTEGER,
                    llm_provider TEXT,
                    llm_model TEXT,
                    request_meta_json TEXT,
                    response_summary_json TEXT,
                    response_json TEXT,
                    error_message TEXT,
                    duration_ms INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_upload_audit_created_at ON upload_audit(created_at DESC)"
            )

    def _headers_identity(self, request: Optional[Request]) -> Dict[str, Optional[str]]:
        if not request:
            return {"name": None, "id": None, "email": None}
        headers = request.headers
        return {
            "name": headers.get("x-mg-user-name")
            or headers.get("x-user-name")
            or headers.get("x-ms-client-principal-name"),
            "id": headers.get("x-mg-user-id") or headers.get("x-user-id"),
            "email": headers.get("x-mg-user-email") or headers.get("x-user-email"),
        }

    def start_upload(
        self,
        *,
        request: Optional[Request],
        file: UploadFile,
        file_bytes: bytes,
        bl_type: Any,
        post_to_dataverse: bool,
        llm_provider: Any = None,
        llm_model: Any = None,
        apply_custom_rules: Optional[bool] = None,
    ) -> str:
        audit_id = str(uuid.uuid4())
        original = file.filename or "upload.bin"
        saved_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{audit_id[:8]}_{_safe_name(original)}"
        saved_path = self.files_dir / saved_name
        saved_path.write_bytes(file_bytes)
        digest = hashlib.sha256(file_bytes).hexdigest()
        identity = self._headers_identity(request)
        endpoint = str(request.url.path) if request else None
        method = request.method if request else None
        client_ip = request.client.host if request and request.client else None
        user_agent = request.headers.get("user-agent") if request else None
        meta = {
            "query": dict(request.query_params) if request else {},
            "apply_custom_rules": apply_custom_rules,
        }
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO upload_audit (
                    id, created_at, status, endpoint, method, uploader_name,
                    uploader_id, uploader_email, client_ip, user_agent,
                    original_filename, saved_filename, saved_path, content_type,
                    file_size, sha256, bl_type, post_to_dataverse, llm_provider,
                    llm_model, request_meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    now,
                    "processing",
                    endpoint,
                    method,
                    identity["name"],
                    identity["id"],
                    identity["email"],
                    client_ip,
                    user_agent,
                    original,
                    saved_name,
                    str(saved_path),
                    file.content_type,
                    len(file_bytes),
                    digest,
                    getattr(bl_type, "value", bl_type),
                    1 if post_to_dataverse else 0,
                    getattr(llm_provider, "value", llm_provider),
                    getattr(llm_model, "value", llm_model),
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
        row = self.get_upload(audit_id, include_response=False)
        self.broadcast(row)
        return audit_id

    def finish_upload(self, audit_id: Optional[str], response: Any, *, started_at: datetime) -> None:
        if not audit_id:
            return
        completed = _utc_now()
        summary = _response_summary(response)
        success = bool(summary.get("success")) and not summary.get("dataverse_error")
        status = "success" if success else "failed"
        error = summary.get("error") or summary.get("dataverse_error")
        duration = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE upload_audit
                SET completed_at=?, status=?, response_summary_json=?,
                    response_json=?, error_message=?, duration_ms=?
                WHERE id=?
                """,
                (
                    completed,
                    status,
                    json.dumps(summary, ensure_ascii=False, default=str),
                    _truncate_json(response),
                    str(error) if error else None,
                    duration,
                    audit_id,
                ),
            )
        row = self.get_upload(audit_id, include_response=False)
        self.broadcast(row)

    def fail_upload(self, audit_id: Optional[str], error: Exception, *, started_at: datetime) -> None:
        if not audit_id:
            return
        duration = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE upload_audit
                SET completed_at=?, status='failed', error_message=?, duration_ms=?,
                    response_summary_json=?, response_json=?
                WHERE id=?
                """,
                (
                    _utc_now(),
                    str(error),
                    duration,
                    json.dumps({"success": False, "error": str(error)}, ensure_ascii=False),
                    json.dumps({"success": False, "error": str(error)}, ensure_ascii=False),
                    audit_id,
                ),
            )
        row = self.get_upload(audit_id, include_response=False)
        self.broadcast(row)

    def _row_to_dict(self, row: sqlite3.Row, *, include_response: bool) -> Dict[str, Any]:
        data = dict(row)
        for key in ("request_meta_json", "response_summary_json"):
            try:
                data[key.replace("_json", "")] = json.loads(data.pop(key) or "{}")
            except json.JSONDecodeError:
                data[key.replace("_json", "")] = {}
        response_json = data.pop("response_json", None)
        if include_response:
            try:
                data["response"] = json.loads(response_json or "{}")
            except json.JSONDecodeError:
                data["response"] = {"raw": response_json}
        data["download_url"] = f"/audit/uploads/{data['id']}/file"
        return data

    def list_uploads(self, *, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM upload_audit ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_dict(row, include_response=False) for row in rows]

    def get_upload(self, audit_id: str, *, include_response: bool = True) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM upload_audit WHERE id=?",
                (audit_id,),
            ).fetchone()
        return self._row_to_dict(row, include_response=include_response) if row else None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        await websocket.send_json({"type": "snapshot", "items": self.list_uploads(limit=50)})

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def _broadcast_async(self, payload: Dict[str, Any]) -> None:
        stale: List[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_json({"type": "upload", "item": payload})
            except Exception:
                stale.append(client)
        for client in stale:
            self.disconnect(client)

    def broadcast(self, payload: Optional[Dict[str, Any]]) -> None:
        if not payload:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast_async(payload))


audit_store = UploadAuditStore()
