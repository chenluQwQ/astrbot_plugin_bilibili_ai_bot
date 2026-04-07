"""
AstrBot Plugin - Bilibili Bot
让你的 AstrBot 成为一个 B站 AI Bot：自动回复评论、主动看视频、发动态。
"""
import io
import os
import re
import time
import json
import asyncio
import hashlib
import requests
import traceback
from datetime import datetime, timedelta
from functools import reduce

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig

# ========== 常量 ==========
PLUGIN_NAME = "astrbot_plugin_bilibili_bot"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REPLIED_FILE = os.path.join(DATA_DIR, "replied.json")
AFFECTION_FILE = os.path.join(DATA_DIR, "affection.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule_today.json")

# B站 API
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_REPLY_URL = "https://api.bilibili.com/x/v2/reply/reply"
BILI_NOTIFY_URL = "https://api.bilibili.com/x/msgfeed/reply"
BILI_COOKIE_INFO_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
BILI_COOKIE_REFRESH_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
BILI_COOKIE_CONFIRM_URL = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# WBI 签名相关
MIXIN_KEY_ENC_TAB = [
    46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,
    33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,
    61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,
    36,20,34,44,52
]

# B站 RSA 公钥（Cookie刷新用）
BILI_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@register(
    "astrbot_plugin_bilibili_bot",
    "chenluQwQ",
    "B站 AI Bot — 自动回复评论、主动看视频、发动态",
    "0.1.0",
    "https://github.com/chenluQwQ/astrbot_plugin_bilibili_bot",
)
class BiliBiliBot(Star):
    """B站 AI Bot 插件 — 评论回复、主动视频、动态发布"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ensure_data_dir()

        # 运行状态
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_cookie_check = 0
        self._login_qrcode_key = None  # 扫码登录用

        # 自动启动后台任务
        if self._has_cookie():
            asyncio.create_task(self._auto_start())

    # ================================================================
    #  工具方法
    # ================================================================

    def _ensure_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)

    def _has_cookie(self) -> bool:
        return bool(self.config.get("SESSDATA", ""))

    def _headers(self) -> dict:
        return {
            "Cookie": f"SESSDATA={self.config.get('SESSDATA', '')}; "
                      f"bili_jct={self.config.get('BILI_JCT', '')}; "
                      f"DedeUserID={self.config.get('DEDE_USER_ID', '')}",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com",
        }

    def _load_json(self, path: str, default=None):
        if default is None:
            default = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _save_json(self, path: str, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ================================================================
    #  B站 Cookie 管理
    # ================================================================

    def check_cookie(self) -> tuple[bool, str]:
        """检查Cookie是否有效，返回 (valid, info)"""
        sessdata = self.config.get("SESSDATA", "")
        if not sessdata:
            return False, "SESSDATA 为空"
        try:
            resp = requests.get(BILI_NAV_URL, headers=self._headers(), timeout=10)
            data = resp.json()
            if data["code"] == 0:
                uname = data["data"].get("uname", "未知")
                mid = data["data"].get("mid", "")
                level = data["data"].get("level_info", {}).get("current_level", 0)
                return True, f"✅ {uname} (UID:{mid}) LV{level}"
            return False, f"❌ Cookie 已失效 (code: {data['code']})"
        except Exception as e:
            return False, f"❌ 检查失败: {e}"

    def check_need_refresh(self) -> tuple[bool, str]:
        """检查Cookie是否需要刷新"""
        bili_jct = self.config.get("BILI_JCT", "")
        try:
            resp = requests.get(
                BILI_COOKIE_INFO_URL,
                params={"csrf": bili_jct},
                headers=self._headers(),
                timeout=10,
            )
            data = resp.json()
            if data["code"] != 0:
                return False, f"检查失败: {data.get('message', '')}"
            if data["data"].get("refresh", False):
                return True, "需要刷新"
            return False, "Cookie 仍然有效"
        except Exception as e:
            return False, f"检查出错: {e}"

    def _generate_correspond_path(self, timestamp_ms: int) -> str:
        """RSA加密生成correspondPath"""
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes, serialization

        public_key = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        plaintext = f"refresh_{timestamp_ms}".encode()
        ciphertext = public_key.encrypt(
            plaintext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return ciphertext.hex()

    def refresh_cookie(self) -> tuple[bool, str]:
        """完整的Cookie刷新流程"""
        rt = self.config.get("REFRESH_TOKEN", "")
        if not rt:
            return False, "没有 REFRESH_TOKEN，无法自动刷新"
        sessdata = self.config.get("SESSDATA", "")
        bili_jct = self.config.get("BILI_JCT", "")
        if not sessdata:
            return False, "SESSDATA 为空"

        try:
            # 1. 检查是否需要刷新
            need, msg = self.check_need_refresh()
            if not need:
                return True, msg

            # 2. 生成 correspondPath
            ts = int(time.time() * 1000)
            correspond_path = self._generate_correspond_path(ts)

            # 3. 获取 refresh_csrf
            url = f"https://www.bilibili.com/correspond/1/{correspond_path}"
            resp = requests.get(url, headers=self._headers(), timeout=10)
            match = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', resp.text)
            if not match:
                return False, "无法提取 refresh_csrf"
            refresh_csrf = match.group(1).strip()

            # 4. 刷新
            resp = requests.post(
                BILI_COOKIE_REFRESH_URL,
                headers=self._headers(),
                data={
                    "csrf": bili_jct,
                    "refresh_csrf": refresh_csrf,
                    "source": "main_web",
                    "refresh_token": rt,
                },
                timeout=10,
            )
            result = resp.json()
            if result["code"] != 0:
                return False, f"刷新失败: {result.get('message', result['code'])}"

            new_rt = result["data"].get("refresh_token", "")
            updates = {}
            if new_rt:
                updates["REFRESH_TOKEN"] = new_rt
            for cookie in resp.cookies:
                if cookie.name == "SESSDATA":
                    updates["SESSDATA"] = cookie.value
                elif cookie.name == "bili_jct":
                    updates["BILI_JCT"] = cookie.value
                elif cookie.name == "DedeUserID":
                    updates["DEDE_USER_ID"] = cookie.value

            if "SESSDATA" not in updates:
                return False, "刷新响应中未找到新 SESSDATA"

            # 5. 确认（用旧 refresh_token）
            try:
                confirm_headers = dict(self._headers())
                confirm_headers["Cookie"] = (
                    f"SESSDATA={updates['SESSDATA']}; "
                    f"bili_jct={updates.get('BILI_JCT', bili_jct)}"
                )
                requests.post(
                    BILI_COOKIE_CONFIRM_URL,
                    headers=confirm_headers,
                    data={"csrf": updates.get("BILI_JCT", bili_jct), "refresh_token": rt},
                    timeout=10,
                )
            except Exception:
                pass

            # 保存新Cookie到配置
            for k, v in updates.items():
                self.config[k] = v
            self.config.save_config()

            return True, f"✅ Cookie 刷新成功！SESSDATA: {updates['SESSDATA'][:8]}..."

        except Exception as e:
            return False, f"刷新出错: {e}"

    # ================================================================
    #  WBI 签名（B站反爬）
    # ================================================================

    def _get_wbi_keys(self) -> tuple[str, str]:
        """获取 wbi_img_key 和 wbi_sub_key"""
        resp = requests.get(BILI_NAV_URL, headers=self._headers(), timeout=10)
        data = resp.json()["data"]["wbi_img"]
        img_url = data["img_url"]
        sub_url = data["sub_url"]
        img_key = img_url.rsplit("/", 1)[1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
        return img_key, sub_key

    def _get_mixin_key(self, orig: str) -> str:
        return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]

    def sign_wbi_params(self, params: dict) -> dict:
        """WBI签名"""
        try:
            img_key, sub_key = self._get_wbi_keys()
            mixin_key = self._get_mixin_key(img_key + sub_key)
            params["wts"] = int(time.time())
            params = dict(sorted(params.items()))
            query = "&".join(f"{k}={v}" for k, v in params.items())
            params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
            return params
        except Exception:
            return params

    # ================================================================
    #  扫码登录
    # ================================================================

    async def _qr_login_generate(self) -> tuple[str | None, str | None]:
        """生成登录二维码，返回 (qr_url, qrcode_key)"""
        try:
            resp = requests.get(BILI_QR_GENERATE_URL, headers={"User-Agent": USER_AGENT}, timeout=10)
            logger.debug(f"[BiliBot] 二维码API响应: status={resp.status_code}, body={resp.text[:200]}")
            data = resp.json()
            if data["code"] == 0:
                return data["data"]["url"], data["data"]["qrcode_key"]
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
        return None, None

    async def _qr_login_poll(self, qrcode_key: str) -> tuple[int, str, dict]:
        """轮询扫码状态，返回 (code, message, cookies_dict)"""
        try:
            resp = requests.get(
                BILI_QR_POLL_URL,
                params={"qrcode_key": qrcode_key},
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            data = resp.json()["data"]
            code = data["code"]
            msg_map = {
                0: "登录成功",
                86038: "二维码已失效",
                86090: "已扫码，请在手机上确认",
                86101: "等待扫码中...",
            }
            cookies = {}
            if code == 0:
                # 从 url 参数中提取 cookie
                url = data.get("url", "")
                refresh_token = data.get("refresh_token", "")
                if url:
                    from urllib.parse import urlparse, parse_qs
                    parsed = parse_qs(urlparse(url).query)
                    cookies = {
                        "SESSDATA": parsed.get("SESSDATA", [""])[0],
                        "bili_jct": parsed.get("bili_jct", [""])[0],
                        "DedeUserID": parsed.get("DedeUserID", [""])[0],
                        "REFRESH_TOKEN": refresh_token,
                    }
                # 也从 response cookies 里取
                for c in resp.cookies:
                    if c.name in ("SESSDATA", "bili_jct", "DedeUserID"):
                        cookies[c.name] = c.value

            return code, msg_map.get(code, f"未知状态({code})"), cookies
        except Exception as e:
            return -1, f"轮询失败: {e}", {}

    # ================================================================
    #  QQ 命令
    # ================================================================

    @filter.command("bili登录")
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录B站，Bot会发送二维码图片"""
        qr_url, qrcode_key = await self._qr_login_generate()
        if not qr_url:
            yield event.plain_result("❌ 生成二维码失败，请稍后重试")
            return

        self._login_qrcode_key = qrcode_key

        # 生成二维码图片
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        # 保存到临时文件
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        with open(qr_path, "wb") as f:
            f.write(buf.getvalue())

        chain = [
            Plain("📱 请用B站APP扫描下方二维码登录：\n扫码后发送 /bili确认 来完成登录"),
            Image.fromFileSystem(qr_path),
        ]
        yield event.chain_result(chain)

    @filter.command("bili确认")
    async def cmd_login_confirm(self, event: AstrMessageEvent):
        """确认扫码登录"""
        if not self._login_qrcode_key:
            yield event.plain_result("❌ 没有待确认的登录，请先发 /bili登录")
            return

        # 轮询几次
        for i in range(3):
            code, msg, cookies = await self._qr_login_poll(self._login_qrcode_key)

            if code == 0:
                # 登录成功，保存Cookie
                if cookies.get("SESSDATA"):
                    self.config["SESSDATA"] = cookies["SESSDATA"]
                if cookies.get("bili_jct"):
                    self.config["BILI_JCT"] = cookies["bili_jct"]
                if cookies.get("DedeUserID"):
                    self.config["DEDE_USER_ID"] = cookies["DedeUserID"]
                if cookies.get("REFRESH_TOKEN"):
                    self.config["REFRESH_TOKEN"] = cookies["REFRESH_TOKEN"]
                self.config.save_config()
                self._login_qrcode_key = None

                # 验证
                valid, info = self.check_cookie()
                yield event.plain_result(f"✅ 登录成功！\n{info}")

                # 自动启动后台任务
                if not self._running:
                    await self._start_bot()
                    yield event.plain_result("🚀 后台任务已自动启动")
                return

            elif code == 86090:
                yield event.plain_result(f"📱 {msg}，请在手机上点确认")
                await asyncio.sleep(2)

            elif code == 86101:
                yield event.plain_result(f"⏳ {msg}")
                await asyncio.sleep(2)

            else:
                self._login_qrcode_key = None
                yield event.plain_result(f"❌ {msg}")
                return

        yield event.plain_result("⏳ 还在等待中，请在手机上确认后再发一次 /bili确认")

    @filter.command("bili状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看Bot运行状态"""
        lines = []

        # Cookie 状态
        valid, info = self.check_cookie()
        lines.append(f"🍪 Cookie: {info}")

        # 运行状态
        lines.append(f"🤖 后台任务: {'运行中 ✅' if self._running else '未运行 ❌'}")

        # 功能开关
        switches = []
        if self.config.get("ENABLE_REPLY", True):
            switches.append("评论回复")
        if self.config.get("ENABLE_PROACTIVE", True):
            switches.append("主动视频")
        if self.config.get("ENABLE_DYNAMIC", True):
            switches.append("动态发布")
        if self.config.get("ENABLE_AFFECTION", True):
            switches.append("好感度")
        lines.append(f"⚙️ 已开启: {', '.join(switches) if switches else '无'}")

        # 今日数据
        schedule = self._load_json(SCHEDULE_FILE)
        if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
            triggered = schedule.get("proactive_triggered", [])
            total = schedule.get("proactive_times", [])
            lines.append(f"📊 今日主动: {len(triggered)}/{len(total)}")
            lines.append(f"📢 今日动态: {'已发' if schedule.get('dynamic_triggered') else '未发'}")

        # 已回复数
        replied = self._load_json(REPLIED_FILE, [])
        if isinstance(replied, list):
            lines.append(f"💬 累计回复: {len(replied)} 条")

        yield event.plain_result("\n".join(lines))

    @filter.command("bili启动")
    async def cmd_start(self, event: AstrMessageEvent):
        """启动后台任务"""
        if self._running:
            yield event.plain_result("⚠️ 已经在运行了")
            return
        if not self._has_cookie():
            yield event.plain_result("❌ 请先用 /bili登录 扫码登录B站")
            return
        await self._start_bot()
        yield event.plain_result("🚀 后台任务已启动！")

    @filter.command("bili停止")
    async def cmd_stop(self, event: AstrMessageEvent):
        """停止后台任务"""
        if not self._running:
            yield event.plain_result("⚠️ 没有在运行")
            return
        await self._stop_bot()
        yield event.plain_result("⏹️ 后台任务已停止")

    @filter.command("bili开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        """切换功能开关。用法: /bili开关 <功能名>"""
        msg = event.message_str.strip()
        # 去掉命令前缀
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            lines = [
                "可切换的功能：",
                f"  回复 — 评论自动回复 ({'✅' if self.config.get('ENABLE_REPLY', True) else '❌'})",
                f"  主动 — 主动看视频 ({'✅' if self.config.get('ENABLE_PROACTIVE', True) else '❌'})",
                f"  动态 — 自动发动态 ({'✅' if self.config.get('ENABLE_DYNAMIC', True) else '❌'})",
                f"  好感 — 好感度系统 ({'✅' if self.config.get('ENABLE_AFFECTION', True) else '❌'})",
                f"  心情 — 心情系统 ({'✅' if self.config.get('ENABLE_MOOD', True) else '❌'})",
                f"  点赞 — 主动点赞 ({'✅' if self.config.get('PROACTIVE_LIKE', True) else '❌'})",
                f"  投币 — 主动投币 ({'✅' if self.config.get('PROACTIVE_COIN', False) else '❌'})",
                f"  收藏 — 主动收藏 ({'✅' if self.config.get('PROACTIVE_FAV', True) else '❌'})",
                f"  关注 — 主动关注 ({'✅' if self.config.get('PROACTIVE_FOLLOW', True) else '❌'})",
                f"  评论 — 主动评论 ({'✅' if self.config.get('PROACTIVE_COMMENT', True) else '❌'})",
                "",
                "用法: /bili开关 回复",
            ]
            yield event.plain_result("\n".join(lines))
            return

        name = parts[1].strip()
        toggle_map = {
            "回复": "ENABLE_REPLY",
            "主动": "ENABLE_PROACTIVE",
            "动态": "ENABLE_DYNAMIC",
            "好感": "ENABLE_AFFECTION",
            "心情": "ENABLE_MOOD",
            "点赞": "PROACTIVE_LIKE",
            "投币": "PROACTIVE_COIN",
            "收藏": "PROACTIVE_FAV",
            "关注": "PROACTIVE_FOLLOW",
            "评论": "PROACTIVE_COMMENT",
        }

        key = toggle_map.get(name)
        if not key:
            yield event.plain_result(f"❌ 不认识的功能名：{name}")
            return

        current = self.config.get(key, True)
        self.config[key] = not current
        self.config.save_config()
        status = "✅ 已开启" if not current else "❌ 已关闭"
        yield event.plain_result(f"{name}: {status}")

    @filter.command("bili刷新")
    async def cmd_refresh_cookie(self, event: AstrMessageEvent):
        """手动刷新B站Cookie"""
        yield event.plain_result("🔄 正在刷新 Cookie...")
        success, msg = self.refresh_cookie()
        yield event.plain_result(msg)

    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = (
            "📺 Bilibili Bot 命令列表\n"
            "━━━━━━━━━━━━━━━━\n"
            "/bili登录 — 扫码登录B站\n"
            "/bili确认 — 确认扫码\n"
            "/bili状态 — 查看运行状态\n"
            "/bili启动 — 启动后台任务\n"
            "/bili停止 — 停止后台任务\n"
            "/bili开关 — 切换功能开关\n"
            "/bili刷新 — 手动刷新Cookie\n"
            "/bili帮助 — 显示本帮助\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 首次使用请先 /bili登录"
        )
        yield event.plain_result(help_text)

    # ================================================================
    #  后台任务
    # ================================================================

    async def _auto_start(self):
        """插件加载时自动启动"""
        await asyncio.sleep(3)  # 等AstrBot初始化完成
        valid, _ = self.check_cookie()
        if valid:
            await self._start_bot()
            logger.info("[BiliBot] Cookie有效，后台任务自动启动")
        else:
            logger.warning("[BiliBot] Cookie无效，等待登录")

    async def _start_bot(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info("[BiliBot] 后台任务启动")

    async def _stop_bot(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[BiliBot] 后台任务停止")

    async def _main_loop(self):
        """主循环 — 评论轮询 + 定时任务调度"""
        logger.info("[BiliBot] 主循环开始")

        while self._running:
            try:
                now = datetime.now()
                hour = now.hour

                # 休眠时段
                sleep_start = self.config.get("SLEEP_START", 2)
                sleep_end = self.config.get("SLEEP_END", 8)
                if sleep_start <= hour < sleep_end:
                    await asyncio.sleep(60)
                    continue

                # 定期检查 Cookie
                cookie_interval = self.config.get("COOKIE_CHECK_INTERVAL", 6) * 3600
                if time.time() - self._last_cookie_check > cookie_interval:
                    await self._check_and_refresh_cookie()
                    self._last_cookie_check = time.time()

                # 评论回复
                if self.config.get("ENABLE_REPLY", True):
                    await self._poll_and_reply()

                # TODO: 主动视频调度
                # if self.config.get("ENABLE_PROACTIVE", True):
                #     await self._maybe_proactive()

                # TODO: 动态发布调度
                # if self.config.get("ENABLE_DYNAMIC", True):
                #     await self._maybe_dynamic()

                poll_interval = self.config.get("POLL_INTERVAL", 20)
                await asyncio.sleep(poll_interval)

            except asyncio.CancelledError:
                logger.info("[BiliBot] 主循环被取消")
                break
            except Exception as e:
                logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(30)

        self._running = False
        logger.info("[BiliBot] 主循环结束")

    async def _check_and_refresh_cookie(self):
        """检查并自动刷新Cookie"""
        valid, info = self.check_cookie()
        if valid:
            logger.info(f"[BiliBot] Cookie 检查通过: {info}")
            return

        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.config.get("COOKIE_AUTO_REFRESH", True):
            success, msg = self.refresh_cookie()
            if success:
                logger.info(f"[BiliBot] Cookie 自动刷新成功: {msg}")
            else:
                logger.error(f"[BiliBot] Cookie 刷新失败: {msg}")

    async def _poll_and_reply(self):
        """轮询B站通知并回复评论（骨架）"""
        try:
            # 获取未读评论通知
            resp = requests.get(
                BILI_NOTIFY_URL,
                headers=self._headers(),
                params={"id": 0, "type": 1},
                timeout=10,
            )
            data = resp.json()
            if data["code"] != 0:
                logger.warning(f"[BiliBot] 获取通知失败: {data.get('message', data['code'])}")
                return

            items = data.get("data", {}).get("items", [])
            if not items:
                return

            replied = set(self._load_json(REPLIED_FILE, []))
            count = 0
            max_replies = self.config.get("MAX_REPLIES_PER_RUN", 3)

            for item in items:
                if count >= max_replies:
                    break

                rpid = str(item.get("id", ""))
                if rpid in replied:
                    continue

                # 提取评论信息
                user = item.get("user", {})
                mid = str(user.get("mid", ""))
                username = user.get("nickname", "")
                content = item.get("item", {}).get("source_content", "")
                oid = item.get("item", {}).get("subject_id", 0)
                comment_type = item.get("item", {}).get("business_id", 1)

                if not content or not rpid:
                    continue

                logger.info(f"[BiliBot] 新评论: {username}({mid}): {content}")

                # 生成AI回复
                ai_reply = await self._generate_reply(content, mid, username)

                if ai_reply:
                    success = self._send_reply(oid, rpid, comment_type, ai_reply)
                    if success:
                        logger.info(f"[BiliBot] 回复成功: {username} <- {ai_reply[:50]}")
                        count += 1
                    else:
                        logger.warning(f"[BiliBot] 回复发送失败: {username}")
                else:
                    logger.warning(f"[BiliBot] 生成回复失败，跳过: {username}")

                replied.add(rpid)
                count += 1

            # 保存已回复列表
            self._save_json(REPLIED_FILE, list(replied))

        except Exception as e:
            logger.error(f"[BiliBot] 轮询出错: {e}")

    # ================================================================
    #  LLM 回复生成
    # ================================================================

    def _get_system_prompt(self) -> str:
        """获取系统提示词：AstrBot人设或自定义"""
        if self.config.get("USE_ASTRBOT_PERSONA", True):
            # 尝试获取 AstrBot 配置的人设
            try:
                personas = self.context.provider_manager.personas
                if personas:
                    # 用第一个人设
                    return personas[0].prompt
            except Exception:
                pass
        # 用自定义提示词
        return self.config.get(
            "CUSTOM_SYSTEM_PROMPT",
            "你是一个B站UP主的AI助手，负责回复评论。回复要简短自然，像真人一样，不要太官方。",
        )

    async def _generate_reply(self, content: str, mid: str, username: str) -> str | None:
        """调用 AstrBot 的 LLM provider 生成回复"""
        try:
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("[BiliBot] 没有可用的 LLM provider")
                return None

            system_prompt = self._get_system_prompt()

            # 构建提示
            owner_name = self.config.get("OWNER_NAME", "") or "主人"
            is_owner = str(mid) == str(self.config.get("OWNER_MID", ""))

            prompt = (
                f"B站用户「{username}」"
                f"{'（这是{owner_name}）' if is_owner else ''}"
                f"在你的视频下评论了：\n"
                f"「{content}」\n\n"
                f"请回复这条评论。要求：简短自然（50字以内），像真人而不是AI，"
                f"不要用emoji过多，不要太官方。只输出回复内容，不要加引号或前缀。"
            )

            max_tokens = self.config.get("REPLY_MAX_TOKENS", 300)

            resp = await provider.text_chat(
                prompt=prompt,
                session_id=f"bili_reply_{mid}",
                contexts=[],
                system_prompt=system_prompt,
            )

            if resp:
                # 兼容不同版本的 AstrBot 响应格式
                reply_text = None
                if hasattr(resp, 'completion_text') and resp.completion_text:
                    reply_text = resp.completion_text
                elif hasattr(resp, 'result_chain') and resp.result_chain:
                    # 从 MessageChain 提取文本
                    for comp in resp.result_chain.chain:
                        if hasattr(comp, 'text') and comp.text:
                            reply_text = comp.text
                            break

                if reply_text:
                    reply = reply_text.strip().strip('"').strip("'")
                    return reply if reply else None

        except Exception as e:
            logger.error(f"[BiliBot] LLM 回复生成失败: {e}\n{traceback.format_exc()}")
        return None

    # ================================================================
    #  B站 API 基础操作（后续填充）
    # ================================================================

    def _send_reply(self, oid: int, rpid: str, reply_type: int, content: str) -> bool:
        """发送评论回复"""
        try:
            resp = requests.post(
                BILI_REPLY_URL,
                headers=self._headers(),
                data={
                    "oid": oid,
                    "type": reply_type,
                    "root": rpid,
                    "parent": rpid,
                    "message": content,
                    "csrf": self.config.get("BILI_JCT", ""),
                },
                timeout=10,
            )
            data = resp.json()
            if data["code"] == 0:
                logger.info(f"[BiliBot] 回复成功: {content[:30]}...")
                return True
            else:
                logger.warning(f"[BiliBot] 回复失败: {data.get('message', data['code'])}")
                return False
        except Exception as e:
            logger.error(f"[BiliBot] 回复出错: {e}")
            return False

    def get_followings(self, mid: str = None) -> list:
        """获取关注列表，默认用Bot自己的"""
        target = mid or self.config.get("DEDE_USER_ID", "")
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/relation/followings",
                headers=self._headers(),
                params={"vmid": target, "ps": 50, "pn": 1},
                timeout=10,
            )
            data = resp.json()
            if data["code"] == 0:
                return [item["mid"] for item in data.get("data", {}).get("list", [])]
        except Exception as e:
            logger.error(f"[BiliBot] 获取关注列表失败: {e}")
        return []

    # ================================================================
    #  生命周期
    # ================================================================

    async def terminate(self):
        """插件卸载/停用时调用"""
        await self._stop_bot()
        logger.info("[BiliBot] 插件已停用")
