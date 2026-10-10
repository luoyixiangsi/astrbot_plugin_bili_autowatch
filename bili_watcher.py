"""刷视频调度器：挑视频、模拟观看、互动、评论。"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from . import bili_client as api


class BiliWatcher:
    """后台循环 + 单次观看会话。

    调度策略：每看完一个视频后，随机等待 interval_min ~ interval_max 秒再刷下一个，
    直到达到每日上限或进入静默时段。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._running = False
        self._last_error = ""
        self._session_count = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return False
        self._stop.clear()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> bool:
        if not self._running:
            return False
        self._stop.set()
        task = self._task
        self._task = None
        self._running = False
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 停止循环时出现异常：{exc}")
        return True

    def status(self) -> dict:
        return {
            "running": self._running,
            "today": self._daily_count(),
            "limit": self._cfg("schedule", "daily_limit", 20),
            "pushed_today": self._push_count(),
            "push_limit": self._push_limit(),
            "last_error": self._last_error,
            "sessions": self._session_count,
        }

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        logger.info("[bili] 后台刷视频循环已启动。")
        try:
            while not self._stop.is_set():
                if self._in_quiet_hours():
                    wait = self._seconds_until_quiet_end()
                    logger.info(f"[bili] 处于静默时段，{wait // 60} 分钟后再继续。")
                    if not await self._sleep(wait):
                        break
                    continue

                if self._daily_count() >= int(self._cfg("schedule", "daily_limit", 20)):
                    logger.info("[bili] 已达到每日上限，等待到明天。")
                    if not await self._sleep(self._seconds_until_tomorrow()):
                        break
                    continue

                try:
                    summary = await self.run_once()
                    if summary and not summary.get("skipped"):
                        self._last_error = str(summary.get("error") or "")
                        await self._notify(
                            self._format_summary(summary), respect_limit=True
                        )
                    else:
                        self._last_error = str((summary or {}).get("reason") or "")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.error(f"[bili] 本轮刷视频失败：{self._last_error}")

                if self._stop.is_set():
                    break
                low = int(self._cfg("schedule", "interval_min", 300))
                high = int(self._cfg("schedule", "interval_max", 900))
                # 间隔为 0 时 random.randint(a, a) 会阻塞 0 秒，属于预期；但不能为负。
                low = max(low, 1)
                interval = random.randint(low, max(low, high))
                logger.info(f"[bili] 等待 {interval} 秒后继续。")
                if not await self._sleep(interval):
                    break
        except asyncio.CancelledError:
            logger.info("[bili] 后台循环被取消。")
            raise
        finally:
            self._running = False
            logger.info("[bili] 后台刷视频循环已退出。")

    # ------------------------------------------------------------------ #
    # 单次观看
    # ------------------------------------------------------------------ #

    async def run_once(self, bvid: str = "", with_review: bool = True) -> dict:
        """挑一个视频并完整走一遍观看流程。

        ``with_review`` 为 False 时跳过观后感生成，供 /bili once 这类
        只想快速看结果的指令使用，避免白花一次模型调用。
        """
        # 同一时间只允许一个观看会话，避免后台循环与手动指令并发导致计数错乱。
        if self._run_lock.locked():
            return {"skipped": True, "reason": "已有一次观看正在进行中"}
        async with self._run_lock:
            result = await self._run_once_inner(bvid)
        # 已经请求停止时不再多花一次模型调用。
        if (
            with_review
            and result
            and not result.get("skipped")
            and not self._stop.is_set()
        ):
            result["review"] = await self._compose_review(result)
        return result

    async def _run_once_inner(self, bvid: str = "") -> dict:
        cfg = self.plugin.cfg
        credential = api.build_credential(cfg)
        api.ensure_request_client(cfg.get("proxy") or "")

        if bvid:
            try:
                brief = await api.find_video_by_bvid(credential, bvid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 找不到视频 {bvid}：{exc}")
                return {"skipped": True, "reason": f"找不到视频 {bvid}：{exc}"}
        else:
            brief = await self._pick_video(credential)

        if brief is None:
            return {"skipped": True, "reason": "没有可选的视频"}

        try:
            brief, info = await api.resolve_video(credential, brief)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 解析视频 {brief.bvid} 失败：{exc}")
            return {"skipped": True, "reason": f"{brief.bvid} 解析失败：{exc}"}
        if not brief.cid:
            return {"skipped": True, "reason": f"{brief.bvid} 没有解析到 cid"}

        duration = int(brief.duration or 0)
        target = self._target_seconds(duration)
        dry_run = bool(cfg.get("dry_run", False))

        result: dict[str, Any] = {
            "bvid": brief.bvid,
            "aid": brief.aid,
            "title": brief.title,
            "owner": brief.owner,
            "duration": duration,
            "watched": target,
            "actions": [],
            "comment": "",
            "tname": str(info.get("tname") or ""),
            "desc": str(info.get("desc") or "").replace("\n", " ")[:200],
            "dry_run": dry_run,
        }

        if dry_run:
            logger.info(
                f"[bili][dry-run] 计划观看 {brief}，目标 {target}s"
            )
            result["actions"].append("dry-run")
            return result

        await self._watch(credential, brief, target, duration, result)

        # 收到停止信号时不再继续点赞/投币/评论。
        if self._stop.is_set():
            return result

        errors: list[str] = []
        await self._interact(credential, brief, result, errors)
        await self._maybe_comment(credential, brief, info, result, errors)
        result["error"] = "；".join(errors)

        self._remember(brief)
        self._bump_daily()
        self._session_count += 1
        self.plugin.store.append(
            "history",
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "bvid": brief.bvid,
                "title": brief.title,
                "owner": brief.owner,
                "watched": result.get("watched", target),
                "actions": result["actions"],
                "comment": result["comment"],
            },
        )
        return result

    async def _watch(
        self,
        credential: Any,
        brief: api.VideoBrief,
        target: int,
        duration: int,
        result: dict,
    ) -> None:
        cfg = self.plugin.cfg
        heartbeat = max(int(self._cfg("watch", "heartbeat_interval", 15)), 5)
        start_ts = int(time.time())
        session = uuid.uuid4().hex
        mid = int(self._credential_mid() or 0)

        if (cfg.get("watch") or {}).get("report_click"):
            try:
                await api.report_click(credential, brief.aid, brief.cid, mid=mid)
                result["actions"].append("点击")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 上报点击失败：{exc}")

        if self._cfg("watch", "report_history", True):
            try:
                await api.report_history(credential, brief.aid, brief.cid, 0)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    f"[bili] 观看进度接口不可用（{exc}）。"
                    "该接口对会话校验较严，心跳接口同样会写入历史记录，已继续。"
                )

        elapsed = 0
        while elapsed < target:
            remaining = target - elapsed
            step = max(int(random.uniform(heartbeat * 0.8, heartbeat * 1.25)), 1)
            # 实际只睡剩余时间，避免 elapsed 虚增导致上报的播放时长大于真实观看时长。
            nap = min(step, remaining)
            if not await self._sleep(nap):
                break
            elapsed += nap
            played = min(elapsed, duration) if duration else elapsed
            try:
                await api.report_heartbeat(
                    credential,
                    aid=brief.aid,
                    bvid=brief.bvid,
                    cid=brief.cid,
                    played_time=played,
                    realtime=elapsed,
                    duration=duration or played,
                    mid=mid,
                    start_ts=start_ts,
                    play_type=0,
                    session=session,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 心跳上报失败：{exc}")

        result["watched"] = elapsed
        if self._cfg("watch", "report_history", True):
            try:
                await api.report_history(
                    credential, brief.aid, brief.cid, min(elapsed, duration or elapsed)
                )
                result["actions"].append("观看进度")
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[bili] 收尾上报观看进度失败：{exc}")

    async def _interact(
        self,
        credential: Any,
        brief: api.VideoBrief,
        result: dict,
        errors: list[str] | None = None,
    ) -> None:
        interact = self.plugin.cfg.get("interact") or {}
        errors = errors if errors is not None else []

        if random.random() < float(interact.get("like_probability") or 0):
            try:
                await api.like_video(credential, brief.bvid, True)
                result["actions"].append("点赞")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 点赞失败：{exc}")
                errors.append(f"点赞失败：{exc}")

        if random.random() < float(interact.get("coin_probability") or 0):
            try:
                await api.coin_video(
                    credential,
                    brief.bvid,
                    num=int(interact.get("coin_num") or 1),
                    also_like=bool(interact.get("coin_also_like", True)),
                )
                result["actions"].append("投币")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 投币失败：{exc}")
                errors.append(f"投币失败：{exc}")

        if random.random() < float(interact.get("favorite_probability") or 0):
            media_id = int(
                interact.get("favorite_media_id")
                or (self.plugin.cfg.get("source_options") or {}).get(
                    "favorite_media_id"
                )
                or 0
            )
            if media_id:
                try:
                    await api.favorite_video(credential, brief.aid, media_id)
                    result["actions"].append("收藏")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili] 收藏失败：{exc}")
                    errors.append(f"收藏失败：{exc}")
            else:
                logger.warning("[bili] 想收藏但没配置收藏夹 media_id，已跳过。")

    async def _maybe_comment(
        self,
        credential: Any,
        brief: api.VideoBrief,
        info: dict,
        result: dict,
        errors: list[str] | None = None,
    ) -> None:
        comment_cfg = self.plugin.cfg.get("comment") or {}
        if not comment_cfg.get("enabled"):
            return
        if random.random() >= float(comment_cfg.get("probability") or 0):
            return

        text = await self._compose_comment(comment_cfg, brief, info)
        if not text:
            return
        try:
            await api.post_comment(credential, brief.aid, text)
            result["comment"] = text
            result["actions"].append("评论")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 评论失败：{exc}")
            if errors is not None:
                errors.append(f"评论失败：{exc}")

    # ------------------------------------------------------------------ #
    # 评论生成
    # ------------------------------------------------------------------ #

    async def _compose_comment(
        self, comment_cfg: dict, brief: api.VideoBrief, info: dict
    ) -> str:
        templates = [
            str(t).strip()
            for t in (comment_cfg.get("templates") or [])
            if str(t).strip()
        ]
        source = str(comment_cfg.get("source") or "llm")

        text = ""
        if source == "llm":
            text = await self._llm_comment(comment_cfg, brief, info)
        if not text and templates:
            text = random.choice(templates)
        if not text:
            return ""

        text = self._sanitize_comment(text, int(comment_cfg.get("max_length") or 40))
        blacklist = [
            str(w).strip().lower()
            for w in (comment_cfg.get("blacklist") or [])
            if str(w).strip()
        ]
        if any(word in text.lower() for word in blacklist):
            logger.warning(f"[bili] 评论命中黑名单词，已改用模板：{text}")
            text = random.choice(templates) if templates else ""
        return text

    async def _llm_comment(
        self, comment_cfg: dict, brief: api.VideoBrief, info: dict
    ) -> str:
        max_length = int(comment_cfg.get("max_length") or 40)
        desc = str(info.get("desc") or "").replace("\n", " ")[:200]
        tname = str(info.get("tname") or "")
        prompt = (
            f"视频标题：{brief.title}\n"
            f"UP主：{brief.owner}\n"
            f"分区：{tname}\n"
            f"简介：{desc}\n\n"
            f"请以观众身份写一条不超过 {max_length} 字的评论。"
            "只输出评论内容本身，不要引号、不要换行、不要解释。"
        )
        try:
            provider_id = str(comment_cfg.get("provider_id") or "").strip()
            if not provider_id:
                provider = self.plugin.context.get_using_provider()
                if provider is None:
                    logger.warning("[bili] 没有可用的 LLM 提供商，评论回退到模板。")
                    return ""
                provider_id = provider.meta().id
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=str(comment_cfg.get("persona") or "") or None,
            )
            return str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 生成评论失败，回退到模板：{exc}")
            return ""

    @staticmethod
    def _sanitize_comment(text: str, max_length: int) -> str:
        text = " ".join(text.split())
        for prefix in ("评论：", "评论:", "回复：", "回复:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        text = text.strip().strip("\"'\u201c\u201d\u2018\u2019\u300a\u300b").strip()
        if len(text) > max_length:
            text = text[:max_length]
        return text

    # ------------------------------------------------------------------ #
    # 选片与状态
    # ------------------------------------------------------------------ #

    async def _compose_review(self, summary: dict) -> str:
        """用大模型给刚看完的视频写一段观后感。

        观后感是发给自己的，允许比评论长得多，因此上限单独用 review.max_length
        控制，并且不做评论那样的黑名单与格式化清洗；生成失败时返回空串，
        由调用方决定如何降级。
        """
        review_cfg = self.plugin.cfg.get("review") or {}
        if not review_cfg.get("enabled", False):
            return ""
        # 演练模式没有真的看视频，没必要浪费一次模型调用。
        if summary.get("dry_run"):
            return ""

        max_length = max(int(review_cfg.get("max_length") or 300), 40)
        desc = str(summary.get("desc") or "").replace("\n", " ")[:300]
        tname = str(summary.get("tname") or "")
        watched = int(summary.get("watched") or 0)
        actions = "、".join(summary.get("actions") or []) or "仅观看"
        comment = str(summary.get("comment") or "")

        prompt = (
            f"视频标题：{summary.get('title')}\n"
            f"UP主：{summary.get('owner')}\n"
            f"分区：{tname}\n"
            f"简介：{desc}\n"
            f"我看了 {watched} 秒（视频总长 {summary.get('duration')} 秒），"
            f"顺手做的操作：{actions}\n"
        )
        if comment:
            prompt += f"我在下面留的评论：{comment}\n"
        prompt += (
            f"\n请以第一人称写一段不超过 {max_length} 字的观后感，"
            "像看完视频后跟朋友随口聊两句那样，说清楚讲了什么、你觉得怎么样。"
            "只输出观后感正文，不要标题、不要引号、不要 markdown、不要复述这段提示。"
        )

        try:
            provider_id = str(review_cfg.get("provider_id") or "").strip()
            if not provider_id:
                provider = self.plugin.context.get_using_provider()
                if provider is None:
                    logger.warning("[bili] 没有可用的 LLM 提供商，跳过观后感。")
                    return ""
                provider_id = provider.meta().id
            resp = await self.plugin.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=str(review_cfg.get("persona") or "") or None,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if len(text) > max_length:
                text = text[:max_length]
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 生成观后感失败：{exc}")
            return ""

    async def _pick_video(self, credential: Any) -> api.VideoBrief | None:
        candidates = await api.fetch_candidates(credential, self.plugin.cfg, 30)
        usable = [c for c in candidates if c.bvid and not self._watched_recently(c.bvid)]
        if not usable:
            usable = [c for c in candidates if c.bvid]
        if not usable:
            return None
        return random.choice(usable)

    def _target_seconds(self, duration: int) -> int:
        cap = max(int(self._cfg("watch", "max_watch_seconds", 180)), 10)
        ratio = float(self._cfg("watch", "watch_ratio", 1.0) or 1.0)
        if duration <= 0:
            return cap
        # 不能超过视频本身时长，否则上报的 realtime 会大于 video_duration。
        return max(min(int(duration * ratio), cap, duration), 1)

    def _state(self) -> dict:
        data = self.plugin.store.load("state", {})
        return data if isinstance(data, dict) else {}

    def _daily_count(self) -> int:
        state = self._state()
        daily = state.get("daily") or {}
        return int(daily.get(self._today(), 0))

    def _bump_daily(self) -> None:
        state = self._state()
        daily = state.get("daily") or {}
        daily[self._today()] = int(daily.get(self._today(), 0)) + 1
        for key in sorted(daily)[:-30]:
            daily.pop(key, None)
        state["daily"] = daily
        self.plugin.store.save("state", state)

    def _push_count(self) -> int:
        state = self._state()
        pushes = state.get("pushes") or {}
        return int(pushes.get(self._today(), 0))

    def _bump_push(self) -> None:
        state = self._state()
        pushes = state.get("pushes") or {}
        pushes[self._today()] = int(pushes.get(self._today(), 0)) + 1
        for key in sorted(pushes)[:-30]:
            pushes.pop(key, None)
        state["pushes"] = pushes
        self.plugin.store.save("state", state)

    def _remember(self, brief: api.VideoBrief) -> None:
        state = self._state()
        watched = state.get("watched") or {}
        watched[brief.bvid] = int(time.time())
        keep_days = int(self._cfg("schedule", "skip_watched_days", 7) or 0)
        if keep_days > 0:
            deadline = int(time.time()) - keep_days * 86400
            watched = {k: v for k, v in watched.items() if int(v) >= deadline}
        state["watched"] = watched
        state["last"] = {
            "bvid": brief.bvid,
            "title": brief.title,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self.plugin.store.save("state", state)

    def _watched_recently(self, bvid: str) -> bool:
        keep_days = int(self._cfg("schedule", "skip_watched_days", 7) or 0)
        if keep_days <= 0:
            return False
        watched = self._state().get("watched") or {}
        ts = watched.get(bvid)
        if not ts:
            return False
        return int(ts) >= int(time.time()) - keep_days * 86400

    def reset_state(self) -> None:
        self.plugin.store.save("state", {})
        self._session_count = 0

    # ------------------------------------------------------------------ #
    # 时间工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _quiet_range(self) -> tuple[timedelta, timedelta] | None:
        raw = str(self._cfg("schedule", "quiet_hours", "") or "").strip()
        if "-" not in raw:
            return None
        start_raw, _, end_raw = raw.partition("-")
        try:
            start = datetime.strptime(start_raw.strip(), "%H:%M")
            end = datetime.strptime(end_raw.strip(), "%H:%M")
        except ValueError:
            logger.warning(f"[bili] 静默时段格式错误，已忽略：{raw}")
            return None
        return (
            timedelta(hours=start.hour, minutes=start.minute),
            timedelta(hours=end.hour, minutes=end.minute),
        )

    def _in_quiet_hours(self) -> bool:
        window = self._quiet_range()
        if not window:
            return False
        now = datetime.now()
        current = timedelta(hours=now.hour, minutes=now.minute)
        start, end = window
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _seconds_until_quiet_end(self) -> int:
        window = self._quiet_range()
        if not window:
            return 60
        _, end = window
        now = datetime.now()
        current = timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)
        delta = end - current
        if delta.total_seconds() <= 0:
            delta += timedelta(days=1)
        return max(int(delta.total_seconds()), 60)

    @staticmethod
    def _seconds_until_tomorrow() -> int:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0
        )
        return max(int((tomorrow - now).total_seconds()), 60)

    async def _sleep(self, seconds: float) -> bool:
        """可被停止信号打断的睡眠。返回 False 表示收到停止信号。"""
        if seconds <= 0:
            return not self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    # ------------------------------------------------------------------ #
    # 其他
    # ------------------------------------------------------------------ #

    def _credential_mid(self) -> int:
        raw = ((self.plugin.cfg.get("credential") or {}).get("dedeuserid") or "").strip()
        return int(raw) if raw.isdigit() else 0

    def _cfg(self, section: str, key: str, default: Any) -> Any:
        data = self.plugin.cfg.get(section) or {}
        value = data.get(key)
        return default if value is None else value

    def _format_summary(self, summary: dict) -> str:
        actions = "、".join(summary.get("actions") or []) or "仅观看"
        lines = [
            "B 站刷视频完成",
            f"标题：{summary.get('title')}",
            f"UP主：{summary.get('owner')}",
            f"观看：{summary.get('watched')}s / 视频 {summary.get('duration')}s",
            f"动作：{actions}",
            f"链接：https://www.bilibili.com/video/{summary.get('bvid')}",
        ]
        if summary.get("comment"):
            lines.append(f"评论：{summary['comment']}")
        review = str(summary.get("review") or "").strip()
        if review:
            lines.append("")
            lines.append("观后感：")
            lines.append(review)
        if summary.get("error"):
            lines.append(f"⚠️ 异常：{summary['error']}")
        lines.append(
            f"今日进度：{self._daily_count()}/{self._cfg('schedule', 'daily_limit', 20)}"
        )
        return "\n".join(lines)

    async def _notify(self, text: str, respect_limit: bool = False) -> bool:
        """把结果推送到播报目标，返回是否真的发出去了。

        ``respect_limit`` 为 True 时（后台自动刷）会先检查每日推送上限；
        手动指令触发的推送不受限，方便随时验证推送链路。
        """
        limit = self._push_limit()
        if respect_limit and limit > 0 and self._push_count() >= limit:
            logger.info(f"[bili] 今日推送已达上限 {limit} 条，本次结果不再推送。")
            return False

        targets = self._notify_targets()
        if not targets:
            logger.warning(
                "[bili] 没有可用的播报目标，结果未推送。"
                "请填写「运行播报会话」，或在管理员列表里加一个会话格式的 UMO。"
            )
            return False
        for umo in targets:
            try:
                await self.plugin.context.send_message(
                    umo, MessageChain().message(text)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili] 推送到 {umo} 失败：{exc}")
                continue
            self._bump_push()
            return True
        return False

    def _push_limit(self) -> int:
        """每日推送上限，0 表示不限。"""
        try:
            return max(int(self.plugin.cfg.get("notify_push_limit") or 0), 0)
        except (TypeError, ValueError):
            logger.warning("[bili] notify_push_limit 不是整数，按不限处理。")
            return 0

    def _notify_targets(self) -> list[str]:
        """按优先级给出播报目标。

        1. 插件配置里的「运行播报会话」；
        2. 配置里的「固定接收 QQ」；
        3. AstrBot 管理员列表里形如 ``default:FriendMessage:123`` 的会话 UMO。
        纯数字的 QQ 号无法直接定位平台，因此不能作为推送目标。
        """
        targets: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in targets:
                targets.append(text)

        add(self.plugin.cfg.get("notify_session"))
        add(self._qq_umo(self.plugin.cfg.get("notify_qq")))

        try:
            admins = self.plugin.context.get_config().get("admins_id") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 读取管理员列表失败：{exc}")
            admins = []
        for admin in admins:
            if str(admin).count(":") >= 2:
                add(admin)
        return targets

    @staticmethod
    def _qq_umo(qq: Any) -> str:
        """把纯数字 QQ 号补成默认平台的私聊 UMO。"""
        raw = str(qq or "").strip()
        if not raw:
            return ""
        if raw.count(":") >= 2:
            return raw
        return f"default:FriendMessage:{raw}" if raw.isdigit() else ""
