from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

import aiohttp

from app.adapters.max.keyboards import MaxKeyboard, to_attachments

logger = logging.getLogger(__name__)


class MaxBotClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://platform-api2.max.ru",
        *,
        upload_retry_attempts: int = 5,
        upload_retry_base_seconds: int = 1,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.upload_retry_attempts = max(1, upload_retry_attempts)
        self.upload_retry_base_seconds = max(0, upload_retry_base_seconds)
        self.timeout = aiohttp.ClientTimeout(total=60)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MaxBotClient":
        self._session = aiohttp.ClientSession(headers={"Authorization": self.token})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("MAX client session is not started")
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = path if path.startswith("http") else self.base_url + path
        retries = 3
        last_payload: Any = None
        for attempt in range(retries):
            async with self.session.request(method, url, **kwargs) as response:
                payload = await response.json(content_type=None)
                last_payload = payload
                if response.status in {429, 503} and attempt + 1 < retries:
                    await asyncio.sleep(1.5 ** attempt)
                    continue
                if response.status >= 400:
                    raise RuntimeError(f"MAX API error {response.status}: {payload}")
                return payload
        raise RuntimeError(f"MAX API error: {last_payload}")

    async def get_me(self) -> dict[str, Any]:
        return await self.request("GET", "/me")

    async def get_updates(
        self,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        update_types = types or ["message_created", "message_callback", "bot_started"]
        params: dict[str, Any] = {"timeout": timeout, "limit": limit, "types": ",".join(update_types)}
        if marker is not None:
            params["marker"] = marker
        return await self.request("GET", "/updates", params=params, timeout=timeout + 10)

    async def get_message(self, message_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/messages/{message_id}")

    async def send_message(
        self,
        chat_id: str | int | None = None,
        user_id: str | int | None = None,
        text: str = "",
        keyboard: MaxKeyboard | None = None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        if chat_id is None and user_id is None:
            raise ValueError("chat_id or user_id is required")
        body: dict[str, Any] = {"text": text, "format": "html"}
        all_attachments = (attachments or []) + (to_attachments(keyboard) or [])
        if all_attachments:
            body["attachments"] = all_attachments
        params: dict[str, Any] = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
        return await self.request("POST", "/messages", params=params, json=body)

    async def answer_callback(self, callback_id: str | None, text: str | None = None) -> None:
        if not callback_id:
            return
        body = {"notification": text or " "}
        try:
            await self.request("POST", "/answers", params={"callback_id": callback_id}, json=body)
        except Exception:
            logger.exception("Failed to answer MAX callback")

    async def get_upload_url(self, upload_type: str) -> dict[str, Any]:
        if upload_type == "photo":
            upload_type = "image"
        return await self.request("POST", "/uploads", params={"type": upload_type})

    async def upload_file(self, path: str | Path, upload_type: str) -> dict[str, Any]:
        path = Path(path)
        upload = await self.get_upload_url(upload_type)
        upload_url = upload.get("url")
        if not upload_url:
            raise RuntimeError("MAX upload URL is empty")
        form = aiohttp.FormData()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form.add_field("data", path.read_bytes(), filename=path.name, content_type=content_type)
        async with self.session.post(upload_url, data=form) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"MAX upload error {response.status}: {payload}")
        if upload.get("token") and "token" not in payload:
            payload["token"] = upload["token"]
        return payload

    async def _send_uploaded(self, chat_id: str | int, path: str | Path, upload_type: str, caption: str | None = None) -> dict[str, Any]:
        uploaded = await self.upload_file(path, upload_type)
        attachment = {"type": upload_type, "payload": uploaded}
        last_error: Exception | None = None
        for attempt in range(self.upload_retry_attempts):
            try:
                return await self.send_message(chat_id=chat_id, text=caption or "", attachments=[attachment])
            except RuntimeError as exc:
                last_error = exc
                if "attachment.not.ready" not in str(exc):
                    raise
                await asyncio.sleep(self.upload_retry_base_seconds * (attempt + 1))
        if last_error:
            raise last_error
        raise RuntimeError("MAX send uploaded file failed")

    async def send_file(self, chat_id: str | int, path: str | Path, caption: str | None = None) -> dict[str, Any]:
        return await self._send_uploaded(chat_id, path, "file", caption)

    async def send_image(self, chat_id: str | int, path: str | Path, caption: str | None = None) -> dict[str, Any]:
        return await self._send_uploaded(chat_id, path, "image", caption)

    async def send_document(self, chat_id: str | int, file_path: str, caption: str | None = None) -> dict[str, Any]:
        suffix = Path(file_path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".heic", ".webp"}:
            return await self.send_image(chat_id, file_path, caption)
        return await self.send_file(chat_id, file_path, caption)

    async def send_document_to_user(self, user_id: str | int, file_path: str, caption: str | None = None) -> dict[str, Any]:
        suffix = Path(file_path).suffix.lower()
        upload_type = 'image' if suffix in {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp', '.heic', '.webp'} else 'file'
        uploaded = await self.upload_file(file_path, upload_type)
        attachment = {'type': upload_type, 'payload': uploaded}
        last_error = None
        for attempt in range(self.upload_retry_attempts):
            try:
                return await self.send_message(user_id=user_id, text=caption or '', attachments=[attachment])
            except RuntimeError as exc:
                last_error = exc
                if 'attachment.not.ready' not in str(exc):
                    raise
                await asyncio.sleep(self.upload_retry_base_seconds * (attempt + 1))
        raise last_error or RuntimeError('MAX send uploaded file failed')


    async def resolve_attachment_url(self, *, token: str | None = None, message_id: str | None = None) -> str | None:
        from app.adapters.max.mapper import parse_update

        if message_id:
            try:
                message = await self.get_message(message_id)
                event = parse_update({"message": message}) or parse_update({"message": message.get("message", {})})
                if event:
                    url = event.photo_url or event.document_url
                    if url:
                        return url
            except Exception:
                logger.exception("Failed to resolve MAX attachment URL by message_id=%s", message_id)
        if token:
            for path in (f"/files/{token}", f"/attachments/{token}", f"/uploads/{token}"):
                try:
                    payload = await self.request("GET", path)
                except Exception:
                    continue
                url = _first_url(payload)
                if url:
                    return url
        return None

    async def download_by_token(self, token: str) -> bytes | None:
        for path in (f"/files/{token}", f"/attachments/{token}", f"/uploads/{token}"):
            url = path if path.startswith("http") else self.base_url + path
            try:
                async with self.session.get(url, timeout=self.timeout) as response:
                    if response.status >= 400:
                        continue
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "json" in content_type or not content_type:
                        payload = await response.json(content_type=None)
                        direct_url = _first_url(payload)
                        if direct_url:
                            return await self.download_file(direct_url)
                        nested_token = _first_token(payload)
                        if nested_token and nested_token != token:
                            nested = await self.download_by_token(nested_token)
                            if nested:
                                return nested
                        continue
                    return await response.read()
            except Exception:
                logger.exception("Failed to download MAX attachment via token endpoint %s", path)
        return None

    async def download_by_id(self, attachment_id: str) -> bytes | None:
        return await self.download_by_token(str(attachment_id))

    async def download_external_url(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self.session.get(url, allow_redirects=True, timeout=self.timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"MAX external download error {response.status}")
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and not any(kind in content_type for kind in ("image/jpeg", "image/jpg", "image/png", "image/webp")):
                logger.debug("MAX external download content-type=%s", content_type)
            destination.write_bytes(await response.read())
        return destination

    async def download_attachment(self, attachment: dict, destination: Path) -> Path:
        payload = attachment.get("payload") or {}
        url = _first_url(payload)
        token = _first_token(payload)
        data: bytes | None = None
        if url:
            await self.download_external_url(url, destination)
            return destination
        elif token:
            data = await self.download_by_token(token)
        if data is None:
            raise RuntimeError("MAX attachment has no downloadable URL")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    async def download_file(self, url: str) -> bytes:
        async with self.session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"MAX download error {response.status}")
            return await response.read()


def _first_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "download_url", "file_url", "image_url", "media_url"):
            if value.get(key):
                return str(value[key])
        for item in value.values():
            url = _first_url(item)
            if url:
                return url
    elif isinstance(value, list):
        for item in value:
            url = _first_url(item)
            if url:
                return url
    return None


def _first_token(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("token", "file_token", "photo_token", "image_token", "media_token", "file_id", "photo_id", "image_id"):
            if value.get(key):
                return str(value[key])
        for item in value.values():
            token = _first_token(item)
            if token:
                return token
    elif isinstance(value, list):
        for item in value:
            token = _first_token(item)
            if token:
                return token
    return None
