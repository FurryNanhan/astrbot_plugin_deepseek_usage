import aiohttp
import datetime
import asyncio
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


class DeepSeekUsagePlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.session = None

    async def initialize(self):
        self.session = aiohttp.ClientSession()

    async def terminate(self):
        if self.session:
            await self.session.close()

    def _format_number(self, num: int) -> str:
        """根据配置格式化数字"""
        fmt = self.config.get("number_format", "comma")
        if fmt == "short":
            if num >= 1_000_000_000:
                return f"{num / 1_000_000_000:.2f} B"
            elif num >= 1_000_000:
                return f"{num / 1_000_000:.2f} M"
            elif num >= 1_000:
                return f"{num / 1_000:.2f} K"
            return str(num)
        else:
            return f"{num:,}"

    def _get_help_text(self):
        return (
            "📖 DeepSeek 用量查询帮助\n"
            "━━━━━━━━━━━━━━\n"
            "🔹 /dsusage ：查询默认天数\n"
            "🔹 /dsusage 7 ：查询最近 7 天\n"
            "🔹 /dsusage today ：查询今天\n"
            "🔹 /dsusage month ：查询本月\n"
            "🔹 /dsusage 10.01 10.31 ：查询指定范围\n"
            "   （支持 . 或 - 分隔，跨年自动识别）\n"
            "━━━━━━━━━━━━━━"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dsusage")
    async def query_usage(self, event: AstrMessageEvent, *args):
        """查询 DeepSeek Token 使用量。输入 /dsusage help 查看帮助。"""
        
        user_token = (self.config.get("deepseek_user_token") or "").strip()
        if not user_token:
            yield event.plain_result("❌ 请先在 WebUI 插件配置中填写 userToken。")
            return

        timeout = self.config.get("request_timeout", 10)
        default_days = self.config.get("default_days", 30)

        start_ts = None
        end_ts = None
        title = ""
        days_count = 0

        # --- 解析逻辑 ---
        if not args:
            days_count = default_days
            title = f"最近 {days_count} 天"
            
        elif len(args) == 1:
            arg = args[0].lower()
            if arg == "help":
                yield event.plain_result(self._get_help_text())
                return
            elif arg == "today":
                days_count = 1
                title = "今天"
            elif arg == "month":
                today = datetime.date.today()
                first_day = today.replace(day=1)
                start_ts = int(datetime.datetime.combine(first_day, datetime.time.min).timestamp())
                end_ts = int(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time.min).timestamp())
                title = "本月"
            elif arg.isdigit():
                days_count = int(arg)
                title = f"最近 {days_count} 天"
            else:
                yield event.plain_result("❌ 输入不规范哦，请按正确格式输入。\n\n" + self._get_help_text())
                return

        elif len(args) == 2:
            date_pattern = r'^(\d{1,2})[\.-](\d{1,2})$'
            m1 = re.match(date_pattern, args[0])
            m2 = re.match(date_pattern, args[1])
            
            if m1 and m2:
                m1_month, m1_day = int(m1.group(1)), int(m1.group(2))
                m2_month, m2_day = int(m2.group(1)), int(m2.group(2))
                
                current_year = datetime.date.today().year
                
                try:
                    start_date = datetime.date(current_year, m1_month, m1_day)
                    end_date = datetime.date(current_year, m2_month, m2_day)
                    
                    # 跨年智能识别
                    if start_date > end_date:
                        start_date = datetime.date(current_year - 1, m1_month, m1_day)
                        
                    start_ts = int(datetime.datetime.combine(start_date, datetime.time.min).timestamp())
                    end_ts = int(datetime.datetime.combine(end_date + datetime.timedelta(days=1), datetime.time.min).timestamp())
                    title = f"{start_date.strftime('%Y.%m.%d')} 至 {end_date.strftime('%Y.%m.%d')}"
                    days_count = (end_date - start_date).days + 1
                except ValueError:
                    yield event.plain_result("❌ 日期不存在（比如填了 2 月 30 号），请重新输入。")
                    return
            else:
                yield event.plain_result("❌ 输入不规范哦，请按正确格式输入。\n\n" + self._get_help_text())
                return
                
        else:
            yield event.plain_result("❌ 参数太多啦，请按格式输入。\n\n" + self._get_help_text())
            return

        # --- 计算时间戳 (针对天数) ---
        if start_ts is None or end_ts is None:
            tz = datetime.timezone(datetime.timedelta(hours=8))
            now = datetime.datetime.now(tz)
            end_dt = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz)
            start_dt = end_dt - datetime.timedelta(days=days_count)
            start_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())

        # --- 查询保护机制 ---
        if days_count > 90:
            yield event.plain_result(f"⏳ 跨度长达 {days_count} 天，正在为您分段查询并累加，可能需要十几秒，请稍候...")

        # --- 执行查询 ---
        usage_lines, usage_ok = await self._query_usage(user_token, timeout, start_ts, end_ts, title)
        yield event.plain_result("\n".join(usage_lines))


    async def _query_usage(self, user_token, timeout, overall_start, overall_end, title):
        user_token = user_token.strip()
        if user_token.startswith("Bearer "):
            user_token = user_token[7:].strip()

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {user_token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://platform.deepseek.com/usage",
        }

        chunk_size = 30 * 86400 # 每次查 30 天
        
        total_input = 0
        total_output = 0
        total_requests = 0
        model_set = set()
        
        current_start = overall_start
        has_error = False

        while current_start < overall_end:
            current_end = min(current_start + chunk_size, overall_end)
            url = f"https://platform.deepseek.com/api/v0/usage/by_api_key/amount?start={current_start}&end={current_end}&tz=28800"
            
            try:
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and data.get("code") == 0:
                            biz_data = data.get("data", {}).get("biz_data", {}) or {}
                            series = biz_data.get("series") or []
                            
                            for s in series:
                                model_set.add(s.get("model", "Unknown"))
                                for bucket in s.get("buckets", []):
                                    usage = bucket.get("usage", {})
                                    total_input += (usage.get("PROMPT_CACHE_HIT_TOKEN", 0) + usage.get("PROMPT_CACHE_MISS_TOKEN", 0))
                                    total_output += usage.get("RESPONSE_TOKEN", 0)
                                    total_requests += usage.get("REQUEST", 0)
                        else:
                            logger.warning(f"分段 {current_start}-{current_end} 返回异常: {data}")
                    else:
                        logger.error(f"分段 {current_start}-{current_end} 请求失败，状态码: {resp.status}")
                        has_error = True
            except Exception as e:
                logger.error(f"分段请求出错 ({current_start}-{current_end}): {e}")
                has_error = True
            
            current_start = current_end
            await asyncio.sleep(0.5)

        total_tokens = total_input + total_output

        if total_tokens == 0:
            return ([f"📭 {title} 没有任何 Token 使用记录。"], True)

        models_str = ", ".join(model_set)
        lines = [
            f"📈 DeepSeek 用量数据（{title}）",
            "━━━━━━━━━━━━━━",
            f"🤖 模型：{models_str}",
            f"🔤 总 Token：{self._format_number(total_tokens)}",
            f"   ⬆️ 输入：{self._format_number(total_input)}",
            f"   ⬇️ 输出：{self._format_number(total_output)}",
            f"🔁 请求次数：{self._format_number(total_requests)} 次",
        ]
        
        if has_error:
            lines.append("⚠️ 注意：部分时间段请求失败，数据可能不完整")
            
        lines.append("━━━━━━━━━━━━━━")
        return (lines, True)