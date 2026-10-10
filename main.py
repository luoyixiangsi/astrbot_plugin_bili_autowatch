"""B站自动刷视频助手 —— AstrBot 插件入口。

指令统一挂在 ``/bili`` 下，用法：

    /bili                 查看帮助
    /bili start           开始后台自动刷
    /bili stop            停止
    /bili status          查看运行状态
    /bili once            立刻刷一个视频
    /bili video BV1xx     指定视频刷一次
    /bili review [BV1xx]  刷完一个视频并把观后感发到当前会话
    /bili comment BV1xx   指定视频发一条评论（用当前评论配置）
    /bili check           校验 Cookie 是否有效
    /bili here            把运行结果推送到当前会话
    /bili reset           清空观看记录与每日计数
"""

from __future__ import annotations

import traceback
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import bili_client as api
from .bili_store import JsonStore
from .bili_watcher import BiliWatcher

PLUGIN_NAME = "astrbot_plugin_bili_autowatch"

HELP_TEXT = """📺 B站自动刷视频助手

/bili start          开始后台自动刷
/bili stop           停止后台刷
/bili status         查看运行状态
/bili once           立刻刷一个视频
/bili video <BV号>   指定视频刷一次
/bili review [BV号]  刷完并把观后感发到当前会话
/bili comment <BV号> 给指定视频发一条评论
/bili check          校验 Cookie 是否有效
/bili here           把结果推送到当前会话
/bili reset          清空观看记录与计数

视频来源、观看时长、点赞投币概率、评论风格等都在插件配置里调整。"""


class BiliAutoWatchPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = JsonStore(PLUGIN_NAME)
        self.watcher = BiliWatcher(self)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def cfg(self) -> Any:
        return self.config

    async def initialize(self) -> None:
        try:
            client = api.ensure_request_client(self.config.get("proxy") or "")
            logger.info(f"[bili] 请求客户端已就绪：{client}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[bili] 初始化请求客户端失败：{exc}")

        if self.config.get("auto_start"):
            if self._has_credential():
                self.watcher.start()
                logger.info("[bili] 已按配置自动开始刷视频。")
            else:
                logger.warning("[bili] 配置了自动开始，但没有填写 Cookie，已跳过。")

    async def terminate(self) -> None:
        await self.watcher.stop()

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command("bili")
    async def bili(
        self, event: AstrMessageEvent, action: str = "", arg: str = ""
    ):
        """B站自动刷视频助手"""
        if self.config.get("admin_only", True) and not event.is_admin():
            yield event.plain_result("只有管理员可以使用该指令。")
            return

        action = (action or "").strip().lower()
        arg = (arg or "").strip()

        try:
            if action in ("", "help", "帮助"):
                yield event.plain_result(HELP_TEXT)
            elif action in ("start", "开", "开始"):
                yield event.plain_result(self._cmd_start())
            elif action in ("stop", "关", "停止"):
                yield event.plain_result(await self._cmd_stop())
            elif action in ("status", "状态"):
                yield event.plain_result(self._cmd_status())
            elif action in ("once", "刷一次"):
                yield event.plain_result("开始刷一个视频，请稍候…")
                yield event.plain_result(await self._cmd_once())
            elif action in ("video", "视频"):
                if not arg:
                    yield event.plain_result("用法：/bili video BV1xxxxxxxxx")
                    return
                yield event.plain_result(f"正在处理 {arg}，请稍候…")
                yield event.plain_result(await self._cmd_once(arg))
            elif action in ("comment", "评论"):
                if not arg:
                    yield event.plain_result("用法：/bili comment BV1xxxxxxxxx")
                    return
                yield event.plain_result(await self._cmd_comment(arg))
            elif action in ("review", "观后感"):
                yield event.plain_result("开始刷视频并生成观后感，请稍候…")
                yield event.plain_result(await self._cmd_review(arg))
            elif action in ("check", "检查"):
                yield event.plain_result(await self._cmd_check())
            elif action in ("here", "这里"):
                yield event.plain_result(self._cmd_here(event))
            elif action in ("reset", "重置"):
                self.watcher.reset_state()
                yield event.plain_result("已清空观看记录与每日计数。")
            else:
                yield event.plain_result(f"未知子命令：{action}\n\n{HELP_TEXT}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[bili] 指令执行失败：{exc}\n{traceback.format_exc()}")
            yield event.plain_result(f"执行失败：{exc}")

    # ------------------------------------------------------------------ #
    # 子命令实现
    # ------------------------------------------------------------------ #

    def _has_credential(self) -> bool:
        cred = self.config.get("credential") or {}
        return bool(str(cred.get("sessdata") or "").strip() and str(cred.get("bili_jct") or "").strip())

    def _cmd_start(self) -> str:
        if not self._has_credential():
            return "还没有配置 Cookie，请先在插件配置里填写 SESSDATA 和 bili_jct。"
        if self.watcher.start():
            return "已开始后台自动刷视频。用 /bili status 查看进度，/bili stop 停止。"
        return "已经在运行中了。"

    async def _cmd_stop(self) -> str:
        if await self.watcher.stop():
            return "已停止后台刷视频。"
        return "当前没有在运行。"

    def _cmd_status(self) -> str:
        state = self.watcher.status()
        credential_state = "已配置" if self._has_credential() else "未配置"
        lines = [
            "📊 B站刷视频状态",
            f"运行中：{'是' if state['running'] else '否'}",
            f"今日已刷：{state['today']}/{state['limit']}",
            f"本次运行累计：{state['sessions']} 个视频",
            f"Cookie：{credential_state}",
            f"演练模式：{'开' if self.config.get('dry_run') else '关'}",
            f"视频来源：{self.config.get('source') or 'hot'}",
            f"自动评论：{'开' if (self.config.get('comment') or {}).get('enabled') else '关'}",
        ]
        push_limit = int(state.get("push_limit") or 0)
        lines.append(
            f"今日推送：{state.get('pushed_today', 0)}"
            f"/{'不限' if push_limit <= 0 else push_limit}"
        )
        if state.get("last_error"):
            lines.append(f"最近错误：{state['last_error']}")
        return "\n".join(lines)

    async def _cmd_once(self, bvid: str = "", with_review: bool = False) -> str:
        if not self._has_credential():
            return "还没有配置 Cookie，请先在插件配置里填写 SESSDATA 和 bili_jct。"
        summary = await self.watcher.run_once(bvid, with_review=with_review)
        if summary.get("skipped"):
            return f"没有执行：{summary.get('reason')}"
        return self.watcher._format_summary(summary)

    async def _cmd_review(self, bvid: str = "") -> str:
        if not self._has_credential():
            return "还没有配置 Cookie，请先在插件配置里填写 SESSDATA 和 bili_jct。"
        summary = await self.watcher.run_once(bvid)
        if summary.get("skipped"):
            return f"没有执行：{summary.get('reason')}"
        review = str(summary.get("review") or "").strip()
        if not review:
            return (
                "看完了，但没能生成观后感。请确认插件配置里「观后感」已开启，"
                "并且有可用的对话模型。"
            )
        return f"《{summary.get('title')}》观后感：\n{review}"

    async def _cmd_comment(self, bvid: str) -> str:
        if not self._has_credential():
            return "还没有配置 Cookie，请先在插件配置里填写 SESSDATA 和 bili_jct。"

        credential = api.build_credential(self.config)
        api.ensure_request_client(self.config.get("proxy") or "")
        brief = await api.find_video_by_bvid(credential, bvid)
        brief, info = await api.resolve_video(credential, brief)

        comment_cfg = dict(self.config.get("comment") or {})
        comment_cfg["enabled"] = True
        text = await self.watcher._compose_comment(comment_cfg, brief, info)
        if not text:
            return "没能生成评论内容，请检查模板配置或 LLM 提供商。"

        if self.config.get("dry_run"):
            return f"[演练模式] 本应评论：{text}"

        await api.post_comment(credential, brief.aid, text)
        return f"已在《{brief.title}》下评论：{text}"

    async def _cmd_check(self) -> str:
        if not self._has_credential():
            return "还没有配置 Cookie，请先在插件配置里填写 SESSDATA 和 bili_jct。"
        credential = api.build_credential(self.config)
        api.ensure_request_client(self.config.get("proxy") or "")
        info = await api.check_credential(credential)
        return (
            f"Cookie 有效 ✅\n昵称：{info.get('name')}\nUID：{info.get('mid')}\n"
            f"等级：{info.get('level')}\n硬币：{info.get('coins')}"
        )

    def _cmd_here(self, event: AstrMessageEvent) -> str:
        umo = event.unified_msg_origin
        self.config["notify_session"] = umo
        try:
            self.config.save_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili] 保存播报会话失败：{exc}")
            return f"已临时记住当前会话，但写入配置失败：{exc}"
        return "已把当前会话设为运行播报目标。"
