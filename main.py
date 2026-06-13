from __future__ import annotations
import asyncio
import re
import calendar
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 东八区时区对象
TZ = timezone(timedelta(hours=8))


# ====================== 定时提醒会话管理 ======================
class ReminderSession:
    def __init__(self, session_id: str, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.reminders: Dict[int, dict] = {}
        self.next_task_id = 1
        self.is_active = True

    async def cleanup(self):
        self.is_active = False
        for reminder in self.reminders.values():
            if reminder["task"] and not reminder["task"].done():
                reminder["task"].cancel()
        self.reminders.clear()

    def add_reminder(self, delay_seconds: int, trigger_time: str,
                     content: str, at_users: List[int]) -> int:
        task_id = self.next_task_id
        self.next_task_id += 1
        self.reminders[task_id] = {
            "task": None,
            "trigger_time": trigger_time,
            "content": content,
            "at_users": at_users
        }
        return task_id

    def remove_reminder(self, task_id: int) -> bool:
        if task_id not in self.reminders:
            return False
        task = self.reminders[task_id]["task"]
        if task and not task.done():
            task.cancel()
        del self.reminders[task_id]
        return True

    def list_reminders(self) -> List[tuple]:
        return [(k, v["trigger_time"], v["content"], v["at_users"])
                for k, v in self.reminders.items()]


# ====================== 核心插件 ======================
@register("timed_reminder", "AstrBot", "ChronoPing", "2.11.0")
class TimedReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.sessions: Dict[str, ReminderSession] = {}
        self.time_format = "%Y-%m-%d %H:%M:%S"

        # 白名单
        self.enable_group_whitelist = self.config.get("enable_group_whitelist", False)
        self.enable_user_whitelist = self.config.get("enable_user_whitelist", False)
        self.group_whitelist = self._parse_whitelist(self.config.get("group_whitelist", []))
        self.user_whitelist = self._parse_whitelist(self.config.get("user_whitelist", []))
        self.max_days = self.config.get("max_reminder_days", 0)

        logger.info(f"[ChronoPing] 白名单配置 - 群组白名单:{self.enable_group_whitelist}，"
                    f"个人白名单:{self.enable_user_whitelist}，最大延迟天数:{self.max_days if self.max_days else '无限制'}")

    def _parse_whitelist(self, value):
        if isinstance(value, list):
            return {int(x) for x in value if str(x).isdigit()}
        elif isinstance(value, str):
            parts = [x.strip() for x in value.split(",") if x.strip().isdigit()]
            return {int(x) for x in parts}
        return set()

    def _get_group_id_safe(self, event: AstrMessageEvent) -> Optional[str]:
        try:
            return event.get_group_id()
        except Exception as e:
            logger.warning(f"[ChronoPing] 获取群组ID失败: {e}")
            return None

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        try:
            if self.enable_group_whitelist:
                group_id = self._get_group_id_safe(event)
                if group_id and int(group_id) not in self.group_whitelist:
                    return False
            if self.enable_user_whitelist:
                if int(event.get_sender_id()) not in self.user_whitelist:
                    return False
            return True
        except Exception as e:
            logger.error(f"[ChronoPing] 权限检查异常: {e}")
            return False

    async def _send_permission_denied(self, event: AstrMessageEvent):
        await event.send(MessageChain([Comp.Plain(text="⛔ 您没有权限使用定时提醒功能，请联系管理员。")]))

    async def get_session(self, event: AstrMessageEvent) -> ReminderSession:
        try:
            sid = event.get_session_id()
        except Exception as e:
            logger.error(f"[ChronoPing] 获取 session_id 失败: {e}")
            raise
        if sid not in self.sessions:
            self.sessions[sid] = ReminderSession(sid, event.get_sender_id())
        return self.sessions[sid]

    # ---------- 中文数字解析增强 ----------
    def _chinese_number_to_int(self, s: str) -> Optional[int]:
        mapping = {
            '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
            '十': 10
        }
        if s in mapping:
            return mapping[s]
        if '十' in s:
            parts = s.split('十', 1)
            left = parts[0]
            right = parts[1] if len(parts) > 1 else ''
            if left == '':
                return 10 + mapping.get(right, 0)
            else:
                tens = mapping.get(left, 0)
                units = mapping.get(right, 0) if right else 0
                return tens * 10 + units
        return None

    def _parse_number(self, s: str) -> Optional[int]:
        if s.isdigit():
            return int(s)
        return self._chinese_number_to_int(s)

    # ---------- 解析纯时间（无日期） ----------
    def _parse_time_only(self, time_str: str) -> Optional[Tuple[int, int, int]]:
        pattern = re.compile(
            r'(上午|下午|晚上|中午)?\s*'
            r'(?:的)?\s*'
            r'((?:\d{1,2})|[零一二三四五六七八九十廿卅]+)'  # 小时
            r'[点时：:]\s*'
            r'(?:'
            r'((?:\d{1,2})|[零一二三四五六七八九十]+)'      # 分钟
            r'分?\s*'
            r'(?:'
            r'((?:\d{1,2})|[零一二三四五六七八九十]+)'      # 秒
            r'秒?'
            r')?'
            r')?',
            re.IGNORECASE
        )
        m = pattern.match(time_str.strip())
        if not m:
            return None
        ampm = m.group(1)
        hour_str = m.group(2)
        minute_str = m.group(3)
        second_str = m.group(4)

        if hour_str.isdigit():
            hour = int(hour_str)
        else:
            hour = self._chinese_number_to_int(hour_str)
            if hour is None:
                return None

        minute = 0
        if minute_str:
            if minute_str.isdigit():
                minute = int(minute_str)
            else:
                minute = self._chinese_number_to_int(minute_str) or 0

        second = 0
        if second_str:
            if second_str.isdigit():
                second = int(second_str)
            else:
                second = self._chinese_number_to_int(second_str) or 0

        if ampm in ('下午', '晚上'):
            if hour < 12:
                hour += 12
        elif ampm == '中午':
            if hour == 0:
                hour = 12
            elif hour < 12:
                hour += 12

        return (hour, minute, second)

    # ---------- 中文口语日期解析（今天/明天/后天）----------
    def _parse_chinese_time(self, time_str: str, base_date):
        m = re.match(r'(今天|明天|后天)', time_str)
        if not m:
            return None
        day_word = m.group(1)
        remaining = time_str[m.end():]
        t = self._parse_time_only(remaining)
        if not t:
            return None
        hour, minute, second = t
        base = base_date
        if day_word == '明天':
            base += timedelta(days=1)
        elif day_word == '后天':
            base += timedelta(days=2)
        return datetime.combine(base, datetime.min.time()).replace(hour=hour, minute=minute, second=second)

    # ---------- 智能时间解析（核心）----------
    def parse_delay_time(self, time_str: str) -> Optional[int]:
        now = datetime.now(TZ).replace(microsecond=0)
        time_str = time_str.strip()

        # 0. 纯数字秒数
        if time_str.isdigit():
            sec = int(time_str)
            return sec if sec > 0 else None

        # 1. 相对时间 + 可选时间描述 (如 "十天后下午三点", "2周后下午三点")
        combined_match = re.match(
            r'((?:\d+)|[零一二三四五六七八九十百千万两]+)\s*'
            r'(天|周|月|年)后\s*'
            r'(.*)', time_str
        )
        if combined_match:
            num = self._parse_number(combined_match.group(1))
            if num is None:
                return None
            unit = combined_match.group(2)
            time_rest = combined_match.group(3).strip()

            if unit == '天':
                days = num
            elif unit == '周':
                days = num * 7
            elif unit == '月':
                days = num * 30
            elif unit == '年':
                days = num * 365
            else:
                return None

            target_date = now.date() + timedelta(days=days)
            if time_rest:
                t = self._parse_time_only(time_rest)
                if not t:
                    return None
                hour, minute, second = t
            else:
                hour, minute, second = now.hour, now.minute, now.second

            target = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second, tzinfo=TZ)
            if target <= now:
                return None
            return int((target - now).total_seconds())

        # 2. 下个月的X号 + 时间 (如 "下个月的14号下午三点")
        next_month_match = re.match(r'下个月的(\d{1,2})[号日]?\s*(.*)', time_str)
        if next_month_match:
            day = int(next_month_match.group(1))
            time_rest = next_month_match.group(2).strip()
            now_date = now.date()
            year = now_date.year
            month = now_date.month + 1
            if month > 12:
                month = 1
                year += 1
            max_day = calendar.monthrange(year, month)[1]
            if day > max_day:
                day = max_day
            target_date = datetime(year, month, day).date()
            if time_rest:
                t = self._parse_time_only(time_rest)
                if not t:
                    return None
                hour, minute, second = t
            else:
                hour, minute, second = now.hour, now.minute, now.second
            target = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second, tzinfo=TZ)
            if target <= now:
                return None
            return int((target - now).total_seconds())

        # 3. "X周后的周X" + 时间 (如 "两周后的周日下午三点")
        weekday_offset_match = re.match(
            r'((?:\d+)|[零一二三四五六七八九十两]+)周后的周([一二三四五六日天])\s*(.*)', time_str
        )
        if weekday_offset_match:
            weeks = self._parse_number(weekday_offset_match.group(1))
            if weeks is None:
                return None
            target_weekday_str = weekday_offset_match.group(2)
            time_rest = weekday_offset_match.group(3).strip()
            weekdays = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6, '天':6}
            if target_weekday_str not in weekdays:
                return None
            target_weekday = weekdays[target_weekday_str]
            base_date = now.date() + timedelta(weeks=weeks)
            current_weekday = base_date.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead < 0:
                days_ahead += 7
            target_date = base_date + timedelta(days=days_ahead)
            if time_rest:
                t = self._parse_time_only(time_rest)
                if not t:
                    return None
                hour, minute, second = t
            else:
                hour, minute, second = now.hour, now.minute, now.second
            target = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second, tzinfo=TZ)
            if target <= now:
                return None
            return int((target - now).total_seconds())

        # 4. 简单相对时间（无后续描述）
        rel_match = re.match(r'(\d+)\s*(秒|分钟|小时|天|周|月)', time_str)
        if rel_match:
            num = int(rel_match.group(1))
            unit = rel_match.group(2)
            if unit == '秒': return num
            elif unit == '分钟': return num * 60
            elif unit == '小时': return num * 3600
            elif unit == '天': return num * 86400
            elif unit == '周': return num * 604800
            elif unit == '月': return num * 2592000

        # 5. 中文口语（今天/明天/后天 + 时间）
        target = self._parse_chinese_time(time_str, now.date())
        if target:
            target = target.replace(tzinfo=TZ)
            if target > now:
                return int((target - now).total_seconds())

        # 6. 下周X、下下周三等
        week_match = re.match(
            r'((?:下)+)?周([一二三四五六日天])'
            r'[^\d]*'
            r'(?:(\d{1,2})[点:：])?'
            r'(?:(\d{1,2})分?)?'
            r'(?:(\d{1,2})秒?)?$',
            time_str
        )
        if week_match:
            xia_prefix = week_match.group(1) or ''
            hour = int(week_match.group(3)) if week_match.group(3) else 0
            minute = int(week_match.group(4)) if week_match.group(4) else 0
            second = int(week_match.group(5)) if week_match.group(5) else 0

            weekdays = {'一':0, '二':1, '三':2, '四':3, '五':4, '六':5, '日':6, '天':6}
            target_weekday = weekdays[week_match.group(2)]
            base_date = now.date()
            days_ahead = target_weekday - now.weekday()
            xia_count = len(xia_prefix) // 1

            if xia_count == 0 and days_ahead <= 0:
                days_ahead += 7
            days_ahead += xia_count * 7

            target_date = base_date + timedelta(days=days_ahead)
            target = datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second, tzinfo=TZ)
            if target <= now:
                return None
            return int((target - now).total_seconds())

        # 7. 简单中文口语（今天19点等）
        chinese_match = re.match(
            r'(今天|明天|后天)'
            r'[^\d]*'
            r'(\d{1,2})[点:：]'
            r'(?:(\d{1,2})'
            r'(?:分)?)?'
            r'(?:(\d{1,2})秒?)?',
            time_str
        )
        if chinese_match:
            day_word, hour_str, min_str, sec_str = chinese_match.groups()
            hour = int(hour_str)
            minute = int(min_str) if min_str else 0
            second = int(sec_str) if sec_str else 0
            base_date = now.date()
            if day_word == '明天':
                base_date += timedelta(days=1)
            elif day_word == '后天':
                base_date += timedelta(days=2)
            target = datetime.combine(base_date, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second, tzinfo=TZ)
            if target <= now:
                return None
            return int((target - now).total_seconds())

        # 8. dateutil 兜底
        try:
            target = date_parser.parse(time_str, fuzzy=True)
            if target.tzinfo is None:
                target = target.replace(tzinfo=TZ)
            else:
                target = target.astimezone(TZ)
            target = target.replace(microsecond=0)
            if target <= now:
                return None
            return int((target - now).total_seconds())
        except:
            pass

        return None

    # ---------- 从消息中提取参数 ----------
    def _extract_args(self, event: AstrMessageEvent) -> tuple:
        full_text = event.message_str.strip()
        cmd = "添加定时提醒"
        if full_text.startswith(cmd):
            args_text = full_text[len(cmd):].strip()
        else:
            args_text = full_text

        quoted_match = re.match(r'["“](.+?)["”]', args_text)
        if not quoted_match:
            quoted_match = re.match(r"['‘](.+?)['’]", args_text)
        if quoted_match:
            time_str = quoted_match.group(1)
            remaining = args_text[quoted_match.end():].strip()
            return (time_str, *self._split_content_at(remaining))

        datetime_match = re.match(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)', args_text)
        if datetime_match:
            time_str = datetime_match.group(1)
            remaining = args_text[datetime_match.end():].strip()
            return (time_str, *self._split_content_at(remaining))

        parts = args_text.split(maxsplit=1)
        time_str = parts[0] if parts else ""
        remaining = parts[1] if len(parts) > 1 else ""
        return (time_str, *self._split_content_at(remaining))

    def _split_content_at(self, text: str) -> tuple:
        match = re.search(r'\s+@([\d,]+)$', text)
        if match:
            content = text[:match.start()].strip()
            at_str = match.group(1)
            return content, at_str
        return text.strip(), ""

    # ---------- 发送提醒 ----------
    async def send_reminder(self, event: AstrMessageEvent,
                            at_users: List[int], content: str):
        if not at_users:
            chain = [Comp.Plain(text=f"【定时提醒】{content}")]
            await event.send(MessageChain(chain))
            return
        for uid in at_users:
            chain = [Comp.At(qq=uid), Comp.Plain(text=f" {content}")]
            await event.send(MessageChain(chain))
            await asyncio.sleep(0.1)

    async def reminder_task(self, event: AstrMessageEvent, task_id: int,
                            at_users: List[int], content: str, delay: int):
        try:
            await asyncio.sleep(delay)
            session = await self.get_session(event)
            if not session.is_active:
                return
            await self.send_reminder(event, at_users, content)
            session.remove_reminder(task_id)
            logger.info(f"[提醒触发] 任务ID:{task_id} | 内容:{content}")
        except asyncio.CancelledError:
            logger.info(f"[任务取消] ID:{task_id}")
        except Exception as e:
            logger.error(f"[任务异常] ID:{task_id} 错误:{e}", exc_info=True)

    # ====================== 命令注册 ======================
    @filter.command("添加定时提醒")
    async def add_reminder(self, event: AstrMessageEvent):
        try:
            if not self._check_permission(event):
                return await self._send_permission_denied(event)

            time_str, content, at_str = self._extract_args(event)
            if not time_str:
                await event.send(MessageChain([Comp.Plain(text="❌ 请输入时间。用法：添加定时提醒 <时间> <内容> [@用户ID]")]))
                return

            delay_sec = self.parse_delay_time(time_str)
            if delay_sec is None:
                help_text = (
                    "❌ 时间格式错误或时间已过！\n"
                    "📌 支持的时间格式：\n"
                    "  · 纯数字秒数：60\n"
                    "  · 相对时间：5分钟 / 2小时 / 3天 / 1周 / 2月\n"
                    "  · 扩展格式：十天后下午三点 / 两周后的周日下午三点 / 下个月的14号下午三点 / 下周三15点\n"
                    "  · 中文口语：今天19点 / 明天下午3点 / 后天上午十点\n"
                    "  · 绝对时间：2026-06-12 18:27:00\n"
                    "  · 含空格的时间无需引号，直接输入即可"
                )
                await event.send(MessageChain([Comp.Plain(text=help_text)]))
                return

            if self.max_days > 0:
                max_seconds = self.max_days * 86400
                if delay_sec > max_seconds:
                    await event.send(MessageChain([Comp.Plain(
                        text=f"❌ 提醒时间不能超过 {self.max_days} 天。当前设置的延迟为 {delay_sec / 86400:.1f} 天。"
                    )]))
                    return

            if not content:
                await event.send(MessageChain([Comp.Plain(text="❌ 请输入提醒内容。")]))
                return

            at_users = []
            if at_str:
                try:
                    at_users = [int(u) for u in at_str.split(",") if u.strip()]
                except ValueError:
                    await event.send(MessageChain([Comp.Plain(text="❌ @用户ID必须是数字，多个用逗号分隔")]))
                    return

            session = await self.get_session(event)
            trigger_time = (datetime.now(TZ) + timedelta(seconds=delay_sec)).strftime(self.time_format)
            task_id = session.add_reminder(delay_sec, trigger_time, content, at_users)

            task = asyncio.create_task(
                self.reminder_task(event, task_id, at_users, content, delay_sec)
            )
            session.reminders[task_id]["task"] = task

            at_tip = f"@用户：{','.join(map(str, at_users))}" if at_users else "无@用户"
            result = f"✅ 提醒添加成功！\n任务ID：{task_id}\n触发时间：{trigger_time}\n{at_tip}\n内容：{content}"
            await event.send(MessageChain([Comp.Plain(text=result)]))
            logger.info(f"[添加提醒] ID:{task_id} | 延迟:{delay_sec}s")
        except Exception as e:
            logger.error(f"[ChronoPing] add_reminder 异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(text="❌ 执行命令时发生未知错误，请查看日志或联系管理员。")]))

    @filter.command("查看定时提醒")
    async def list_reminders(self, event: AstrMessageEvent):
        try:
            if not self._check_permission(event):
                return await self._send_permission_denied(event)
            session = await self.get_session(event)
            tasks = session.list_reminders()
            if not tasks:
                await event.send(MessageChain([Comp.Plain(text="📭 暂无提醒任务")]))
                return
            text = "📋 提醒任务列表：\n"
            for tid, t_time, content, at_users in tasks:
                at_tip = f"@{','.join(map(str, at_users))}" if at_users else "无@"
                text += f"ID:{tid} | {t_time} | {content} | {at_tip}\n"
            await event.send(MessageChain([Comp.Plain(text=text)]))
        except Exception as e:
            logger.error(f"[ChronoPing] list_reminders 异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(text="❌ 查询提醒失败，请稍后重试。")]))

    @filter.command("删除定时提醒")
    async def del_reminder(self, event: AstrMessageEvent):
        try:
            if not self._check_permission(event):
                return await self._send_permission_denied(event)
            args = event.message_str.strip().split()
            if len(args) < 2:
                await event.send(MessageChain([Comp.Plain(text="❌ 请输入任务ID。用法：删除定时提醒 <任务ID>")]))
                return
            task_id = args[1]
            tid = int(task_id)
            session = await self.get_session(event)
            ok = session.remove_reminder(tid)
            text = "✅ 删除成功！" if ok else "❌ 任务不存在"
            await event.send(MessageChain([Comp.Plain(text=text)]))
        except ValueError:
            await event.send(MessageChain([Comp.Plain(text="❌ 任务ID必须是数字")]))
        except Exception as e:
            logger.error(f"[ChronoPing] del_reminder 异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(text="❌ 删除失败，请稍后重试。")]))

    @filter.command("清空定时提醒")
    async def clear_reminders(self, event: AstrMessageEvent):
        try:
            if not self._check_permission(event):
                return await self._send_permission_denied(event)
            session = await self.get_session(event)
            await session.cleanup()
            await event.send(MessageChain([Comp.Plain(text="🗑️ 已清空所有提醒任务")]))
        except Exception as e:
            logger.error(f"[ChronoPing] clear_reminders 异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(text="❌ 清空失败，请稍后重试。")]))

    @filter.command("立即提醒")
    async def now_reminder(self, event: AstrMessageEvent):
        try:
            if not self._check_permission(event):
                return await self._send_permission_denied(event)
            full_text = event.message_str.strip()
            cmd = "立即提醒"
            if full_text.startswith(cmd):
                args_text = full_text[len(cmd):].strip()
            else:
                args_text = full_text
            if not args_text:
                await event.send(MessageChain([Comp.Plain(text="❌ 请输入提醒内容。")]))
                return
            content, at_str = self._split_content_at(args_text)
            at_users = []
            if at_str:
                try:
                    at_users = [int(u) for u in at_str.split(",") if u.strip()]
                except ValueError:
                    await event.send(MessageChain([Comp.Plain(text="❌ @用户ID必须是数字")]))
                    return
            await self.send_reminder(event, at_users, content)
            await event.send(MessageChain([Comp.Plain(text="📢 提醒已发送！")]))
        except Exception as e:
            logger.error(f"[ChronoPing] now_reminder 异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(text="❌ 发送失败，请稍后重试。")]))

    async def shutdown(self):
        for session in self.sessions.values():
            await session.cleanup()
        self.sessions.clear()
        logger.info("[ChronoPing] 插件已卸载，所有任务已清理")