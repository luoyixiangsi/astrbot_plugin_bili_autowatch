"""B 站接口封装层。

这一层只负责"怎么调 B 站"，不含任何调度、概率、策略逻辑。

关于上报观看：B 站网页端刷视频时依次调用三个接口

1. ``x/click-interface/click/web/h5``  上报点击（计入播放量，有风控限制）
2. ``x/click-interface/web/heartbeat`` 播放心跳，默认 15 秒一次
3. ``x/v2/history/report``             上报观看进度（决定历史记录）

``bilibili-api-python`` 没有封装这几个接口，这里直接用它的 ``Api`` 类发请求，
这样能自动复用凭据、CSRF、wbi 与连接池。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from bilibili_api import (
    Credential,
    favorite_list,
    get_registered_clients,
    homepage,
    hot,
    rank,
    request_settings,
    search,
    select_client,
)
from bilibili_api import user as bili_user
from bilibili_api import video as bili_video
from bilibili_api.comment import CommentResourceType, send_comment
from bilibili_api.rank import RankType
from bilibili_api.search import SearchObjectType
from bilibili_api.utils.network import Api
from bilibili_api.exceptions import NetworkException

from astrbot.api import logger

HISTORY_REPORT_URL = "https://api.bilibili.com/x/v2/history/report"
WEB_HEARTBEAT_URL = "https://api.bilibili.com/x/click-interface/web/heartbeat"
WEB_CLICK_URL = "https://api.bilibili.com/x/click-interface/click/web/h5"
_CLIENT_PRIORITY = ("curl_cffi", "aiohttp", "httpx")


class BiliError(RuntimeError):
    """业务层可读错误。"""


@dataclass
class VideoBrief:
    """候选视频的轻量描述，``cid`` 为 0 表示还没解析。"""

    aid: int = 0
    bvid: str = ""
    cid: int = 0
    title: str = ""
    duration: int = 0
    owner: str = ""
    source: str = ""

    def __str__(self) -> str:
        minutes, seconds = divmod(max(self.duration, 0), 60)
        return f"{self.title}（{self.owner} / {minutes}分{seconds}秒 / {self.bvid}）"


def ensure_request_client(proxy: str = "") -> str:
    """挑一个可用的 HTTP 客户端注册给 bilibili-api。

    bilibili-api 17.x 默认不注册任何客户端，必须显式选择，否则所有请求都会报错。
    """
    available = get_registered_clients()
    for name in _CLIENT_PRIORITY:
        if name in available:
            select_client(name)
            if proxy:
                request_settings.set_proxy(proxy)
            return name
    raise BiliError(
        "bilibili-api 没有可用的请求客户端，请安装 curl_cffi / aiohttp / httpx 之一。"
    )


def build_credential(cfg: dict) -> Credential:
    cred = cfg.get("credential") or {}
    return Credential(
        sessdata=(cred.get("sessdata") or "").strip() or None,
        bili_jct=(cred.get("bili_jct") or "").strip() or None,
        buvid3=(cred.get("buvid3") or "").strip() or None,
        dedeuserid=(cred.get("dedeuserid") or "").strip() or None,
        proxy=(cfg.get("proxy") or "").strip() or None,
    )


async def check_credential(credential: Credential) -> dict:
    """校验凭据是否有效，返回账号信息。"""
    info = await _with_retry(lambda: bili_user.get_self_info(credential))
    if not isinstance(info, dict) or not info.get("mid"):
        raise BiliError("凭据无效，请检查 SESSDATA 是否过期。")
    return info


# --------------------------------------------------------------------------- #
# 候选视频
# --------------------------------------------------------------------------- #


async def _fill_cid(credential: Credential, brief: VideoBrief) -> VideoBrief:
    """搜索/UP 主接口不返回 cid，需要单独查一次。"""
    if brief.cid and brief.aid:
        return brief
    video = bili_video.Video(bvid=brief.bvid, credential=credential)
    try:
        info = await video.get_info()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[bili] 获取视频详情失败，改从分 P 接口取 cid：{exc}")
        pages = await video.get_pages()
        if not pages:
            raise BiliError(f"{brief.bvid} 无法解析 cid") from exc
        info = {"cid": pages[0].get("cid"), "duration": pages[0].get("duration")}
    brief.aid = brief.aid or int(info.get("aid") or 0)
    brief.cid = brief.cid or int(info.get("cid") or 0)
    brief.duration = brief.duration or int(info.get("duration") or 0)
    if not brief.title:
        brief.title = str(info.get("title") or "")
    if not brief.owner:
        owner = info.get("owner") or {}
        brief.owner = str(owner.get("name") or "")
    return brief


def _from_standard_item(item: dict, source: str) -> VideoBrief:
    owner = item.get("owner") or item.get("upper") or {}
    return VideoBrief(
        aid=int(item.get("aid") or 0),
        bvid=str(item.get("bvid") or ""),
        cid=int(item.get("cid") or 0),
        title=str(item.get("title") or "").strip(),
        duration=int(item.get("duration") or 0),
        owner=str(owner.get("name") or ""),
        source=source,
    )


async def fetch_candidates(
    credential: Credential,
    cfg: dict,
    limit: int = 20,
) -> list[VideoBrief]:
    """按配置的来源拉取候选视频（带风控重试）。"""
    return await _with_retry(lambda: _fetch_candidates_once(credential, cfg, limit))


async def _fetch_candidates_once(
    credential: Credential,
    cfg: dict,
    limit: int = 20,
) -> list[VideoBrief]:
    source = str(cfg.get("source") or "hot")
    options = cfg.get("source_options") or {}
    page = int(options.get("page") or 1)

    if source == "hot":
        data = await hot.get_hot_videos(pn=max(page, 1), ps=limit)
        return [_from_standard_item(i, source) for i in (data.get("list") or [])]

    if source == "rank":
        data = await rank.get_rank(RankType.All)
        return [_from_standard_item(i, source) for i in (data.get("list") or [])][:limit]

    if source == "recommend":
        data = await homepage.get_videos(credential=credential)
        briefs = []
        for item in data.get("item") or []:
            # 首页推荐里 bvid 在顶层，id 字段是 aid；非 av 卡片（直播/广告）直接跳过。
            if item.get("goto") != "av" or not item.get("bvid"):
                continue
            briefs.append(
                VideoBrief(
                    aid=int(item.get("id") or 0),
                    bvid=str(item.get("bvid") or ""),
                    cid=int(item.get("cid") or 0),
                    title=str(item.get("title") or "").strip(),
                    duration=int(item.get("duration") or 0),
                    owner=str((item.get("owner") or {}).get("name") or ""),
                    source=source,
                )
            )
        return briefs[:limit]

    if source == "search":
        keywords = [
            k.strip()
            for k in str(options.get("keywords") or "").replace("，", ",").split(",")
            if k.strip()
        ]
        if not keywords:
            raise BiliError("视频来源是 search，但没配置搜索关键词。")
        keyword = _pick(keywords)
        data = await search.search_by_type(
            keyword,
            search_type=SearchObjectType.VIDEO,
            page=max(page, 1),
            page_size=max(limit, 20),
        )
        briefs = []
        for item in data.get("result") or []:
            # 搜索结果混有课程/广告等非视频卡片，只保留 type == video。
            if item.get("type") != "video" or not item.get("bvid"):
                continue
            briefs.append(
                VideoBrief(
                    aid=int(item.get("aid") or 0),
                    bvid=str(item.get("bvid") or ""),
                    title=_strip_em(str(item.get("title") or "")).strip(),
                    duration=_parse_duration(item.get("duration")),
                    owner=str(item.get("author") or ""),
                    source=source,
                )
            )
        return briefs[:limit]

    if source == "favorite":
        media_id = int(options.get("favorite_media_id") or 0)
        if not media_id:
            raise BiliError("视频来源是 favorite，但没配置收藏夹 media_id。")
        data = await favorite_list.get_video_favorite_list_content(
            media_id, credential=credential
        )
        briefs = []
        for item in data.get("medias") or []:
            briefs.append(
                VideoBrief(
                    aid=int(item.get("id") or 0),
                    bvid=str(item.get("bvid") or ""),
                    title=str(item.get("title") or "").strip(),
                    duration=int(item.get("duration") or 0),
                    owner=str((item.get("upper") or {}).get("name") or ""),
                    source=source,
                )
            )
        return briefs[:limit]

    if source == "up":
        mids = [
            m.strip()
            for m in str(options.get("up_mids") or "").replace("，", ",").split(",")
            if m.strip()
        ]
        if not mids:
            raise BiliError("视频来源是 up，但没配置 UP 主 UID。")
        uid = int(_pick(mids))
        uploader = bili_user.User(uid, credential=credential)
        data = await uploader.get_videos(pn=max(page, 1), ps=max(limit, 30))
        vlist = (data.get("list") or {}).get("vlist") or []
        return [
            VideoBrief(
                aid=int(item.get("aid") or 0),
                bvid=str(item.get("bvid") or ""),
                title=str(item.get("title") or "").strip(),
                duration=_parse_duration(item.get("length")),
                owner=str(item.get("author") or ""),
                source=source,
            )
            for item in vlist
        ][:limit]

    raise BiliError(f"未知的视频来源：{source}")


async def resolve_video(
    credential: Credential, brief: VideoBrief
) -> tuple[VideoBrief, dict]:
    """补全 cid / 时长，并返回视频详情。

    列表接口通常已经给出 cid 和时长，这里只在缺失时才补请求；
    详情接口失败不会中断流程，调用方拿到空 dict 也能继续观看。
    """
    return await _with_retry(lambda: _resolve_video_once(credential, brief))


async def _resolve_video_once(
    credential: Credential, brief: VideoBrief
) -> tuple[VideoBrief, dict]:
    brief = await _fill_cid(credential, brief)
    try:
        video = bili_video.Video(aid=brief.aid, credential=credential)
        info = await video.get_info()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[bili] 获取视频详情失败，使用列表数据继续：{exc}")
        return brief, {}
    brief.duration = brief.duration or int(info.get("duration") or 0)
    if not brief.title:
        brief.title = str(info.get("title") or "")
    if not brief.owner:
        brief.owner = str((info.get("owner") or {}).get("name") or "")
    return brief, info


async def find_video_by_bvid(credential: Credential, bvid: str) -> VideoBrief:
    async def _fetch() -> VideoBrief:
        video = bili_video.Video(bvid=bvid.strip(), credential=credential)
        info = await video.get_info()
        return VideoBrief(
            aid=int(info.get("aid") or 0),
            bvid=str(info.get("bvid") or bvid),
            cid=int(info.get("cid") or 0),
            title=str(info.get("title") or ""),
            duration=int(info.get("duration") or 0),
            owner=str((info.get("owner") or {}).get("name") or ""),
            source="manual",
        )

    return await _with_retry(_fetch)


# --------------------------------------------------------------------------- #
# 观看上报
# --------------------------------------------------------------------------- #


async def report_history(
    credential: Credential, aid: int, cid: int, progress: int = 0
) -> dict:
    """上报观看进度，决定该视频是否进入历史记录。"""
    async def _send() -> dict:
        api = Api(
            url=HISTORY_REPORT_URL, method="POST", verify=True, credential=credential
        )
        return await api.update_data(
            aid=aid, cid=cid, progress=max(int(progress), 0), platform="android"
        ).request()

    return await _with_retry(_send)


async def report_click(
    credential: Credential,
    aid: int,
    cid: int,
    mid: int = 0,
    part: int = 1,
) -> dict:
    """上报点击，会计入播放量。B 站对此有风控，慎用。"""
    async def _send() -> dict:
        now = int(time.time())
        api = Api(
            url=WEB_CLICK_URL,
            method="POST",
            no_csrf=True,
            credential=credential,
        )
        return await api.update_data(
            mid=mid,
            aid=aid,
            cid=cid,
            part=part,
            lv=0,
            ftime=now,
            stime=now,
            type=3,
            sub_type=0,
            outer=0,
            spmid="333.788.0.0",
        ).request()

    return await _with_retry(_send)


async def report_heartbeat(
    credential: Credential,
    *,
    aid: int,
    bvid: str,
    cid: int,
    played_time: int,
    realtime: int,
    duration: int,
    mid: int = 0,
    start_ts: int = 0,
    play_type: int = 0,
    session: str = "",
) -> dict:
    """播放心跳，网页端约 15 秒一次。"""
    api = Api(
        url=WEB_HEARTBEAT_URL, method="POST", verify=True, credential=credential
    )
    params = {
        "w_start_ts": start_ts,
        "w_mid": mid,
        "w_aid": aid,
        "w_dt": 2,
        "w_realtime": realtime,
        "w_playedtime": played_time,
        "w_real_played_time": realtime,
        "w_video_duration": duration,
        "w_last_play_progress_time": played_time,
        "web_location": 1315873,
    }
    data = {
        "aid": aid,
        "bvid": bvid,
        "cid": cid,
        "mid": mid,
        "played_time": played_time,
        "realtime": realtime,
        "real_played_time": realtime,
        "video_duration": duration,
        "last_play_progress_time": played_time,
        "max_play_progress_time": played_time,
        "start_ts": start_ts,
        "type": 3,
        "sub_type": 0,
        "dt": 2,
        "outer": 0,
        "spmid": "333.788.0.0",
        "from_spmid": "333.999.0.0",
        "session": session,
        "play_type": play_type,
        "refer_url": f"https://www.bilibili.com/video/{bvid}",
        "quality": 80,
        "extra": json.dumps({"player_version": "4.8.36"}),
    }
    return await api.update_params(**params).update_data(**data).request()


# --------------------------------------------------------------------------- #
# 互动
# --------------------------------------------------------------------------- #


async def like_video(credential: Credential, bvid: str, status: bool = True) -> dict:
    return await _with_retry(
        lambda: bili_video.Video(bvid=bvid, credential=credential).like(status)
    )


async def coin_video(
    credential: Credential, bvid: str, num: int = 1, also_like: bool = False
) -> dict:
    return await _with_retry(
        lambda: bili_video.Video(bvid=bvid, credential=credential).pay_coin(
            max(1, min(int(num), 2)), like=also_like
        )
    )


async def triple_video(credential: Credential, bvid: str) -> dict:
    return await _with_retry(
        lambda: bili_video.Video(bvid=bvid, credential=credential).triple()
    )


async def favorite_video(credential: Credential, aid: int, media_id: int) -> dict:
    """收藏视频。直接复用 bilibili-api 的 ``set_favorite``，避免自己拼参数出错。"""
    return await _with_retry(
        lambda: bili_video.Video(aid=aid, credential=credential).set_favorite(
            add_media_ids=[int(media_id)]
        )
    )


async def post_comment(
    credential: Credential,
    aid: int,
    text: str,
    root: int | None = None,
    parent: int | None = None,
) -> dict:
    """发表评论。

    ``send_comment`` 的签名是 ``(text, oid, type_, ...)``，oid 是位置参数，
    这里统一用关键字调用避免顺序写错。
    """
    return await _with_retry(
        lambda: send_comment(
            text=text,
            oid=aid,
            type_=CommentResourceType.VIDEO,
            root=root,
            parent=parent,
            credential=credential,
        )
    )


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


async def _with_retry(factory, attempts: int = 3, base_delay: float = 2.0):
    """带退避的重试。

    B 站对高频请求会返回 412（风控）或 429，这类错误重试一次通常就能过；
    业务错误（-400/-403 等）不重试，直接抛给调用方。
    """
    import asyncio

    if attempts < 1:
        raise ValueError("attempts 必须大于等于 1")

    # 只在 412（WAF 拦截）/429/5xx 时重试：这些情况下请求没有被业务层处理，
    # 所以即使是点赞、投币这类写操作，重试也不会产生重复副作用。
    last_exc: Exception | None = None
    for index in range(attempts):
        await _throttle()
        try:
            return await factory()
        except NetworkException as exc:
            if getattr(exc, "status", None) not in _RETRY_STATUS:
                raise
            last_exc = exc
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "412" not in message and "429" not in message:
                raise
            last_exc = exc
        if index < attempts - 1:
            await asyncio.sleep(base_delay * (2**index))
    if last_exc:
        raise last_exc
    raise BiliError("请求失败且没有捕获到异常信息")


_RETRY_STATUS = (412, 429, 500, 502, 503, 504)
_MIN_REQUEST_INTERVAL = 0.5
_last_request_at = 0.0


async def _throttle() -> None:
    """全局请求节流，降低触发 412 风控的概率。"""
    global _last_request_at
    import asyncio
    import time

    now = time.monotonic()
    wait = _MIN_REQUEST_INTERVAL - (now - _last_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_at = time.monotonic()


def _pick(items: list[Any]) -> Any:
    import random

    return random.choice(items)


def _strip_em(text: str) -> str:
    import re

    return re.sub(r"</?em[^>]*>", "", text)


def _parse_duration(value: Any) -> int:
    """搜索接口返回 'MM:SS' 或 'HH:MM:SS'，投稿接口返回秒数。"""
    if isinstance(value, int):
        return value
    if not value:
        return 0
    parts = str(value).split(":")
    try:
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    except ValueError:
        return 0
