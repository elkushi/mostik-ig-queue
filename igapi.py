"""Minimal client for the Instagram API with Instagram Login (graph.instagram.com).

Stdlib only, so it runs the same on the laptop and in GitHub Actions.
The HTTP call is injectable (`transport`) so the tests never touch the network.

Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing/
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

REEL_METRICS = ["views", "reach", "saved", "shares", "likes", "comments", "total_interactions", "ig_reels_avg_watch_time"]
POST_METRICS = ["views", "reach", "saved", "shares", "likes", "comments", "total_interactions", "follows", "profile_visits"]


class IgError(RuntimeError):
    pass


def urllib_transport(method: str, url: str, data: dict | None, timeout: int = 60) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            msg = json.loads(raw).get("error", {})
            text = f"{msg.get('type', 'Error')} {msg.get('code', '')}: {msg.get('message', raw)}"
        except ValueError:
            text = raw[:300]
        raise IgError(f"HTTP {e.code} {method} {url.split('?')[0]} → {text}") from None
    except urllib.error.URLError as e:
        raise IgError(f"network error {method} {url.split('?')[0]}: {e.reason}") from None


class Instagram:
    def __init__(self, token: str, user_id: str = "me", host: str = "https://graph.instagram.com",
                 version: str = "", transport=urllib_transport, sleep=time.sleep):
        if not token:
            raise IgError("IG_TOKEN is empty")
        self.token, self.user_id = token, user_id or "me"
        self.base = host.rstrip("/") + (f"/{version}" if version else "")
        self.host = host.rstrip("/")
        self.t, self.sleep = transport, sleep

    # -- low level -------------------------------------------------------
    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self.t("GET", f"{self.base}/{path}?{urllib.parse.urlencode(params)}", None)

    def post(self, path: str, **data) -> dict:
        data["access_token"] = self.token
        return self.t("POST", f"{self.base}/{path}", data)

    # -- account ---------------------------------------------------------
    def whoami(self) -> dict:
        return self.get("me", fields="user_id,username,account_type,followers_count,media_count")

    def refresh_token(self) -> dict:
        """New long-lived token, valid 60 days. The current one must be 24 h+ old."""
        url = f"{self.host}/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": self.token})
        return self.t("GET", url, None)

    def publishing_limit(self) -> dict:
        return self.get(f"{self.user_id}/content_publishing_limit", fields="quota_usage,config")

    # -- publishing ------------------------------------------------------
    def wait_ready(self, container_id: str, timeout_s: int = 600, every_s: int = 5) -> None:
        waited = 0
        while True:
            st = self.get(container_id, fields="status_code,status").get("status_code")
            if st == "FINISHED":
                return
            if st in ("ERROR", "EXPIRED"):
                info = self.get(container_id, fields="status").get("status", "")
                raise IgError(f"container {container_id} {st}: {info}")
            if waited >= timeout_s:
                raise IgError(f"container {container_id} not ready after {timeout_s}s (last: {st})")
            self.sleep(every_s)
            waited += every_s

    def _publish(self, container_id: str) -> dict:
        media_id = self.post(f"{self.user_id}/media_publish", creation_id=container_id)["id"]
        try:
            link = self.get(media_id, fields="permalink").get("permalink")
        except IgError:
            link = None
        return {"media_id": media_id, "permalink": link}

    def publish_carousel(self, image_urls: list[str], caption: str) -> dict:
        if not 2 <= len(image_urls) <= 10:
            raise IgError(f"a carousel needs 2–10 images, got {len(image_urls)}")
        children = []
        for url in image_urls:
            cid = self.post(f"{self.user_id}/media", image_url=url, is_carousel_item="true")["id"]
            children.append(cid)
        for cid in children:
            self.wait_ready(cid, timeout_s=180)
        parent = self.post(f"{self.user_id}/media", media_type="CAROUSEL", children=",".join(children), caption=caption)["id"]
        self.wait_ready(parent, timeout_s=180)
        return self._publish(parent)

    def publish_image(self, image_url: str, caption: str) -> dict:
        cid = self.post(f"{self.user_id}/media", image_url=image_url, caption=caption)["id"]
        self.wait_ready(cid, timeout_s=180)
        return self._publish(cid)

    def publish_reel(self, video_url: str, caption: str, cover_url: str | None = None) -> dict:
        data = {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"}
        if cover_url:
            data["cover_url"] = cover_url
        cid = self.post(f"{self.user_id}/media", **data)["id"]
        self.wait_ready(cid, timeout_s=900, every_s=10)
        return self._publish(cid)

    # -- numbers ---------------------------------------------------------
    def media_insights(self, media_id: str, kind: str) -> dict:
        metrics = REEL_METRICS if kind == "reel" else POST_METRICS
        try:
            rows = self.get(f"{media_id}/insights", metric=",".join(metrics)).get("data", [])
        except IgError:
            rows = []  # one unsupported metric fails the whole call: ask one by one
            for m in metrics:
                try:
                    rows += self.get(f"{media_id}/insights", metric=m).get("data", [])
                except IgError:
                    pass
        out = {}
        for r in rows:
            vals = r.get("values") or [{}]
            out[r.get("name")] = r.get("total_value", {}).get("value", vals[0].get("value"))
        return out
