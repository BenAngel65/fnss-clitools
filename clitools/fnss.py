"""Minimal REST client for fast-note-sync-service (fnss).

Endpoints used by fnss-clitools:
    GET    /api/note                 -> fetch one note
    POST   /api/note                 -> create / overwrite one note
    DELETE /api/note                 -> soft-delete (moves to recycle bin)
    DELETE /api/note/recycle-clear   -> hard-delete from recycle bin
    PUT    /api/note/restore         -> restore from recycle bin
    GET    /api/notes                -> list / search notes

fnss uses a `Token` header (not Authorization) for auth. Always returns HTTP
200; success is signalled by the body's `code` field:
    1 = Success
    2 = Created
    3 = Updated
    4 = Deleted
    5 = PasswordUpdated
    6 = NoUpdate
Note-not-found is reported as code=430 (inside HTTP 200).
"""
from __future__ import annotations

from typing import Optional

import requests

from . import __version__

SUCCESS_CODES = {1, 2, 3, 4, 5, 6}
NOT_FOUND_CODES = {404, 430}  # 430 = NoteNotFound (real fnss); 404 for legacy mocks


class FnssError(Exception):
    """Raised on any fnss API failure."""


class FnssClient:
    def __init__(self, host: str, token: str, timeout: int = 10):
        if not host:
            raise FnssError("host is empty")
        if not token:
            raise FnssError("token is empty")
        self.host = host.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Token": self.token,
            "x-client": "fnss-clitools",
            "x-client-version": __version__,
            "User-Agent": f"fnss-clitools/{__version__}",
        }

    def _request(self, method: str, endpoint: str, params=None, data=None) -> dict:
        url = f"{self.host}{endpoint}"
        try:
            resp = requests.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=data,
                timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as e:
            raise FnssError(f"无法连接 {self.host}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise FnssError(f"请求超时 ({self.timeout}s)") from e
        except requests.exceptions.RequestException as e:
            raise FnssError(f"网络错误: {e}") from e

        try:
            payload = resp.json()
        except ValueError as e:
            raise FnssError(f"非 JSON 响应 (HTTP {resp.status_code}): {resp.text[:200]}") from e

        # fnss normally returns HTTP 200 with code in body. Real 4xx/5xx surfaces here.
        if resp.status_code >= 500:
            raise FnssError(
                f"HTTP {resp.status_code}: {payload.get('message') or resp.text[:200]}"
            )
        return payload

    # ---------- helpers ----------

    def _check_success(self, payload: dict, op: str) -> dict:
        code = payload.get("code")
        if code in NOT_FOUND_CODES:
            return None  # not-found is a normal case for some operations
        if code not in SUCCESS_CODES:
            raise FnssError(
                f"{op} 失败: code={code} {payload.get('message')}"
            )
        return payload.get("data") or {}

    # ---------- note endpoints ----------

    def get_note(self, vault: str, path: str) -> Optional[dict]:
        """Fetch note metadata + content. Returns None if not found (code 430)."""
        payload = self._request(
            "GET", "/api/note", params={"vault": vault, "path": path}
        )
        code = payload.get("code")
        if code in NOT_FOUND_CODES:
            return None
        if code not in SUCCESS_CODES:
            raise FnssError(f"GET 失败: code={code} {payload.get('message')}")
        return payload.get("data") or {}

    def write_note(self, vault: str, path: str, content: str) -> dict:
        """Overwrite note content. Returns server response data."""
        payload = self._request(
            "POST",
            "/api/note",
            data={"vault": vault, "path": path, "content": content},
        )
        code = payload.get("code")
        if code not in SUCCESS_CODES:
            raise FnssError(f"写入失败: code={code} {payload.get('message')}")
        return payload.get("data") or {}

    def delete_note(self, vault: str, path: str) -> dict:
        """Soft-delete (move to recycle bin)."""
        payload = self._request(
            "DELETE", "/api/note", data={"vault": vault, "path": path}
        )
        return self._check_success(payload, "删除")

    def recycle_clear(self, vault: str, path: str) -> dict:
        """Hard-delete from recycle bin."""
        payload = self._request(
            "DELETE",
            "/api/note/recycle-clear",
            data={"vault": vault, "path": path},
        )
        return self._check_success(payload, "硬删")

    def restore_note(self, vault: str, path: str) -> dict:
        """Restore a note from the recycle bin."""
        payload = self._request(
            "PUT", "/api/note/restore", data={"vault": vault, "path": path}
        )
        return self._check_success(payload, "恢复")

    # ---------- list / search ----------

    def list_notes(
        self,
        vault: str,
        keyword: Optional[str] = None,
        search_mode: str = "path",
        page: int = 1,
        page_size: int = 100,
    ) -> dict:
        """List notes (with optional keyword search).

        search_mode: 'path' | 'content' | 'regex'
        Returns: {"list": [...], "pager": {...}}
        Each item in list has path, pathHash, version, ctime, mtime, size, ...
        but NOT content (use get_note to fetch body).
        """
        params: dict = {
            "vault": vault,
            "page": page,
            "pageSize": page_size,
        }
        if keyword:
            params["keyword"] = keyword
            params["searchMode"] = search_mode
        payload = self._request("GET", "/api/notes", params=params)
        code = payload.get("code")
        if code not in SUCCESS_CODES:
            raise FnssError(f"列表失败: code={code} {payload.get('message')}")
        data = payload.get("data") or {}
        return {
            "list": data.get("list", []),
            "pager": data.get(
                "pager",
                {"page": page, "pageSize": page_size, "totalRows": 0},
            ),
        }