#!/usr/bin/env python3
"""
Witchly.host MC 服务器自动监控脚本 —— SeleniumBase UC Mode 版
功能：
  1. SeleniumBase UC Mode 自动绕过 Cloudflare Turnstile
  2. Discord Token 注入登录
  3. 检测服务器状态，离线自动启动
  4. Stability 剩余 < 3 天自动续期（扣 500 Coins）
"""

import os
import re
import sys
import json
import time
import traceback
from pathlib import Path
from urllib.request import Request, urlopen

from seleniumbase import SB

# ── 环境变量 ──────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get("WITCHLY_DISCORD_TOKEN", "").strip()
SERVER_ID            = os.environ.get("WITCHLY_SERVER_ID", "").strip()
TG_BOT_TOKEN         = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID           = os.environ.get("TG_CHAT_ID", "").strip()
RENEW_THRESHOLD_DAYS = float(os.environ.get("RENEW_THRESHOLD_DAYS", "3"))

BASE_URL       = "https://dash.witchly.host"
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ── 日志 ──────────────────────────────────────────────────
def log(msg):  print(f"[INFO]  {msg}", flush=True)
def warn(msg): print(f"[WARN]  {msg}", flush=True)
def err(msg):  print(f"[ERROR] {msg}", flush=True)

# ── Telegram 推送 ─────────────────────────────────────────
def send_tg(text: str, img_path: str | None = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        warn("TG 未配置，跳过推送")
        return
    try:
        if img_path and Path(img_path).exists():
            img_bytes = Path(img_path).read_bytes()
            boundary  = "----WitchlyBoundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{text}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urlopen(req, timeout=20):
            log("TG 推送成功")
    except Exception as e:
        warn(f"TG 推送失败: {e}")

# ── 截图 ──────────────────────────────────────────────────
def snap(sb, name: str) -> str | None:
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        sb.save_screenshot(path)
        log(f"截图: {path}")
        return path
    except Exception as e:
        warn(f"截图失败: {e}")
        return None

# ── 等待 URL 包含关键字 ───────────────────────────────────
def wait_for_url(sb, keyword: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if keyword in sb.get_current_url():
            return True
        time.sleep(0.5)
    return False

# ── Cloudflare Turnstile 处理 ─────────────────────────────
def handle_cloudflare(sb):
    """
    UC Mode 内建绕过机制：
    - uc_open_with_reconnect 打开页面时已自动处理大多数情况
    - uc_gui_click_captcha   专门针对 Turnstile 复选框，模拟真人轨迹点击
    """
    url = sb.get_current_url()

    # 检查是否停在 Challenge 页
    is_challenge = (
        "challenge" in url
        or "turnstile" in url.lower()
        or sb.is_element_present("iframe[src*='challenges.cloudflare.com']")
    )

    if is_challenge:
        log("检测到 Cloudflare Turnstile，UC Mode 自动处理...")
        try:
            sb.uc_gui_click_captcha()   # 模拟真人点击 Turnstile 复选框
            log("Turnstile 处理完毕")
        except Exception as e:
            warn(f"uc_gui_click_captcha 异常: {e}")
        time.sleep(3)

# ── Discord OAuth 授权 ────────────────────────────────────
def handle_oauth(sb):
    log("处理 Discord OAuth 授权...")
    time.sleep(2)
    for _ in range(12):
        if "discord.com" not in sb.get_current_url():
            return
        # 滚动到底部让授权按钮可见
        sb.execute_script("""
            document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) el.scrollTop = el.scrollHeight;
            });
            window.scrollTo(0, document.body.scrollHeight);
        """)
        time.sleep(0.8)
        for sel in [
            'button:contains("Authorize")',
            'button:contains("授权")',
            'button[type="submit"]',
        ]:
            try:
                if not sb.is_element_visible(sel):
                    continue
                text = sb.get_text(sel).strip().lower()
                if any(k in text for k in ("cancel", "deny", "取消")):
                    continue
                sb.uc_click(sel)        # uc_click 模拟真人点击，防止 Discord 检测 bot
                log(f"已授权: {text!r}")
                time.sleep(2)
                if "discord.com" not in sb.get_current_url():
                    return
                break
            except Exception:
                continue
        time.sleep(1.5)

# ── Discord Token 注入登录 ────────────────────────────────
def discord_login(sb):
    log("打开 Witchly 首页...")
    # uc_open_with_reconnect：先以普通方式打开，检测到 Cloudflare 后自动断连重连绕过
    sb.uc_open_with_reconnect(BASE_URL, reconnect_time=4)
    time.sleep(3)
    handle_cloudflare(sb)
    time.sleep(2)

    log(f"当前页面: {sb.get_current_url()}")

    # 点击 Discord 登录按钮
    clicked = False
    for sel in [
        'button:contains("Sign In with Discord")',
        'a:contains("Sign In with Discord")',
        'button:contains("Login with Discord")',
        'a:contains("Login with Discord")',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)        # uc_click 绕过点击检测
                log(f"点击: {sel}")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # 兜底
        try:
            sb.uc_click('[class*="discord"]')
            clicked = True
        except Exception:
            pass

    if not clicked:
        snap(sb, "login-btn-not-found")
        raise RuntimeError("未找到 Discord 登录按钮，请查看截图")

    # 等待跳转到 Discord
    if not wait_for_url(sb, "discord.com", timeout=20):
        snap(sb, "discord-redirect-failed")
        raise RuntimeError("未能跳转到 Discord，当前: " + sb.get_current_url())

    log("已到达 Discord，注入 Token...")

    # 通过 localStorage 注入 Token（与 FreezeHost 方案相同原理）
    sb.execute_script("""
        var token = arguments[0];
        var f = document.createElement('iframe');
        f.style.display = 'none';
        document.body.appendChild(f);
        f.contentWindow.localStorage.setItem('token', '"' + token + '"');
        try { localStorage.setItem('token', '"' + token + '"'); } catch(e) {}
        document.body.removeChild(f);
    """, DISCORD_TOKEN)

    sb.refresh()
    time.sleep(4)

    if "discord.com/login" in sb.get_current_url():
        snap(sb, "token-invalid")
        raise RuntimeError("Discord Token 无效或已过期，请重新获取")

    log("Token 注入成功")

    # 处理 OAuth 授权页
    if "discord.com/oauth2/authorize" in sb.get_current_url():
        handle_oauth(sb)

    # 等待跳回 Witchly
    if not wait_for_url(sb, "witchly.host", timeout=25):
        if "discord.com" in sb.get_current_url():
            handle_oauth(sb)
        if not wait_for_url(sb, "witchly.host", timeout=15):
            snap(sb, "not-witchly")
            raise RuntimeError("未能跳回 Witchly，当前: " + sb.get_current_url())

    # Witchly 本身可能也有 Cloudflare 验证
    handle_cloudflare(sb)
    time.sleep(2)
    log(f"✅ 登录成功！当前: {sb.get_current_url()}")

# ── 解析 Stability 时间 ───────────────────────────────────
def parse_stability_days(text: str) -> float | None:
    if not text:
        return None
    t = text.lower().strip()
    d = re.search(r"(\d+)\s*d", t)
    h = re.search(r"(\d+)\s*h", t)
    m = re.search(r"(\d+)\s*m", t)
    total = (int(d.group(1)) if d else 0) \
          + (int(h.group(1)) if h else 0) / 24.0 \
          + (int(m.group(1)) if m else 0) / 1440.0
    return total if total > 0 else None

def fmt_days(v: float) -> str:
    d, h = int(v), int((v - int(v)) * 24)
    return f"{d}d {h}h" if d > 0 else f"{h}h"

# ── 读取 My Servers 页信息 ────────────────────────────────
def get_server_info(sb) -> dict:
    log("打开 My Servers 页...")
    sb.uc_open_with_reconnect(f"{BASE_URL}/servers", reconnect_time=3)
    time.sleep(4)
    handle_cloudflare(sb)
    time.sleep(2)

    info = sb.execute_script(f"""
        var SERVER_ID = "{SERVER_ID}";
        var card = null;
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            var t = el.innerText || '';
            if (t.includes(SERVER_ID) && el.children.length > 0 && el.children.length < 30)
                card = el;
        }}
        var root = card || document.body;

        // 状态检测
        var status = 'unknown';
        var allEls = root.querySelectorAll('*');
        for (var i = 0; i < allEls.length; i++) {{
            var el = allEls[i];
            var bg  = getComputedStyle(el).backgroundColor || '';
            var cls = (el.className || '').toString();
            if (bg.match(/rgb\\(34,\\s*197/) || cls.includes('green') || cls.includes('online'))
                {{ status = 'online'; break; }}
            if (bg.match(/rgb\\(239,\\s*68/) || cls.includes('offline') || cls.includes('stopped'))
                {{ status = 'offline'; break; }}
        }}
        if (status === 'unknown') {{
            var bodyText = (root.innerText || '').toLowerCase();
            if (/\\bonline\\b/.test(bodyText))  status = 'online';
            else if (/\\boffline\\b/.test(bodyText)) status = 'offline';
        }}

        // Stability 文字
        var stabilityText = '';
        for (var i = 0; i < allEls.length; i++) {{
            var el = allEls[i];
            if (el.children.length > 0) continue;
            var t = (el.innerText || '').trim();
            if (/^\\d+d\\s*\\d*h?$/.test(t) || /^\\d+h$/.test(t))
                {{ stabilityText = t; break; }}
        }}
        if (!stabilityText) {{
            for (var i = 0; i < allEls.length; i++) {{
                var el = allEls[i];
                if ((el.innerText || '').trim().toUpperCase() === 'STABILITY') {{
                    var p = el.parentElement;
                    if (p) {{
                        var sib = p.querySelector('p, span, b, strong');
                        if (sib) stabilityText = sib.innerText.trim();
                    }}
                    break;
                }}
            }}
        }}

        return {{ status: status, stabilityText: stabilityText }};
    """)

    stab_days = parse_stability_days(info.get("stabilityText", ""))
    stab_str  = f"{info.get('stabilityText','?')}" + (f" ({fmt_days(stab_days)})" if stab_days else "")
    log(f"状态: {info['status']}  |  Stability: {stab_str}")

    return {
        "status":         info["status"],
        "stability_text": info.get("stabilityText", ""),
        "stability_days": stab_days,
    }

# ── 检测控制台电源状态 ────────────────────────────────────
def get_power_status(sb) -> str:
    url = f"{BASE_URL}/servers/{SERVER_ID}/manage/console"
    log(f"打开控制台: {url}")
    sb.uc_open_with_reconnect(url, reconnect_time=3)
    time.sleep(5)
    handle_cloudflare(sb)
    time.sleep(2)

    status = sb.execute_script("""
        // 找纯文本徽章 RUNNING / OFFLINE 等
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.children.length > 0) continue;
            var t = (el.innerText || '').trim().toUpperCase();
            if (t === 'RUNNING')  return 'running';
            if (t === 'OFFLINE')  return 'offline';
            if (t === 'STARTING') return 'starting';
            if (t === 'STOPPING') return 'stopping';
        }
        // 找按钮
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').toLowerCase().trim();
            if (t === 'stop' || t === 'stop server' || t === 'restart') return 'running';
            if (t === 'start' || t === 'start server')                   return 'offline';
        }
        // 正文兜底
        var body = document.body.innerText.toLowerCase();
        if (/\\brunning\\b/.test(body))               return 'running';
        if (/\\boffline\\b|\\bstopped\\b/.test(body)) return 'offline';
        if (/\\bstarting\\b/.test(body))              return 'starting';
        if (/\\bstopping\\b/.test(body))              return 'stopping';
        return 'unknown';
    """)

    log(f"电源状态: {status}")
    return status or "unknown"

# ── 启动服务器 ────────────────────────────────────────────
def start_server(sb) -> bool:
    log("点击 Start 按钮...")
    for sel in [
        'button:contains("Start Server")',
        'button:contains("Start")',
        '[role="button"]:contains("Start")',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"已点击: {sel}")
                return True
        except Exception:
            continue

    result = sb.execute_script("""
        var btns = document.querySelectorAll('button, [role="button"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').toLowerCase().trim();
            if (t === 'start' || t === 'start server') {
                btns[i].click();
                return 'clicked:' + btns[i].innerText.trim();
            }
        }
        return 'not_found';
    """)
    log(f"JS Start 结果: {result}")
    return "not_found" not in str(result)

# ── 续期 Extend Realm Life ────────────────────────────────
def renew_server(sb) -> bool:
    log("执行续期...")
    sb.uc_open_with_reconnect(f"{BASE_URL}/servers", reconnect_time=3)
    time.sleep(3)
    handle_cloudflare(sb)
    time.sleep(2)

    # 点击 STABILITY 区紫色续期按钮
    clicked = sb.execute_script(f"""
        var SERVER_ID = "{SERVER_ID}";
        var card = null;
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.innerText || '').includes(SERVER_ID) &&
                el.children.length > 0 && el.children.length < 30)
                card = el;
        }}
        var root = card || document.body;

        // 方法1：紫色背景按钮
        var btns = root.querySelectorAll('button, [role="button"]');
        for (var i = 0; i < btns.length; i++) {{
            var btn = btns[i];
            var cls = (btn.className || '').toString();
            var bg  = getComputedStyle(btn).backgroundColor || '';
            if (cls.includes('purple') || cls.includes('violet') ||
                bg.match(/rgb\\(139,\\s*92/) || bg.match(/rgb\\(124,\\s*58/)) {{
                btn.click();
                return 'purple-btn';
            }}
        }}

        // 方法2：STABILITY 标签旁的按钮
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.innerText || '').trim().toUpperCase() === 'STABILITY') {{
                var area = el.closest('[class]') || el.parentElement;
                if (area) {{
                    var ab = area.querySelectorAll('button, [role="button"]');
                    if (ab.length > 0) {{
                        ab[ab.length - 1].click();
                        return 'stability-btn';
                    }}
                }}
            }}
        }}

        // 方法3：含时钟 SVG、title 含 renew/extend 的按钮
        for (var i = 0; i < btns.length; i++) {{
            var btn = btns[i];
            if (btn.querySelector('svg')) {{
                var label = (btn.title || btn.getAttribute('aria-label') || '').toLowerCase();
                if (/renew|extend|stab/.test(label)) {{
                    btn.click();
                    return 'svg-btn:' + label;
                }}
            }}
        }}

        return 'not_found';
    """)

    log(f"续期按钮: {clicked}")
    if "not_found" in str(clicked):
        warn("未找到续期按钮")
        snap(sb, "renew-not-found")
        return False

    time.sleep(2)  # 等弹窗

    # 点击弹窗 Proceed
    for sel in [
        'button:contains("Proceed")',
        'button[class*="purple"]',
        'button[class*="bg-purple"]',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"已点击 Proceed: {sel}")
                time.sleep(3)
                return True
        except Exception:
            continue

    # JS 兜底
    r = sb.execute_script("""
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            if ((btns[i].innerText || '').toLowerCase().includes('proceed')) {
                btns[i].click();
                return 'js-proceed';
            }
        }
        return 'not_found';
    """)
    if "not_found" not in str(r):
        log(f"JS Proceed: {r}")
        time.sleep(3)
        return True

    warn("未找到 Proceed 按钮")
    snap(sb, "proceed-not-found")
    return False

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少: WITCHLY_DISCORD_TOKEN")
    if not SERVER_ID:
        raise RuntimeError("缺少: WITCHLY_SERVER_ID")

    log(f"▶ 监控服务器 [{SERVER_ID}]，续期阈值 < {RENEW_THRESHOLD_DAYS}d")
    messages = []

    # ── SeleniumBase UC Mode 关键参数 ────────────────────
    # uc=True            启用 Undetected Chrome，伪装真实浏览器指纹
    # headless=True      无头模式（GitHub Actions 必须）
    # uc_cdp_events=True 监听 CDP 事件，增强反检测能力
    # chromium_arg       CI 环境必要的 Chrome 启动参数
    with SB(
        uc=True,
        headless=True,
        uc_cdp_events=True,
        chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu",
    ) as sb:
        try:
            # ① 登录
            discord_login(sb)
            snap(sb, "01-after-login")

            # ② 读取服务器信息（Stability + 状态）
            info           = get_server_info(sb)
            stability_days = info["stability_days"]
            stability_text = info["stability_text"]
            snap(sb, "02-my-servers")

            # ③ 续期检查
            if stability_days is not None and stability_days < RENEW_THRESHOLD_DAYS:
                log(f"⚠ Stability {fmt_days(stability_days)} < {RENEW_THRESHOLD_DAYS}d，触发续期")
                ok = renew_server(sb)
                snap(sb, "03-after-renew")
                if ok:
                    new_info = get_server_info(sb)
                    msg = (f"🔄 续期成功！\n"
                           f"之前: {stability_text}\n"
                           f"现在: {new_info['stability_text']}"
                           + (f" ({fmt_days(new_info['stability_days'])})"
                              if new_info['stability_days'] else ""))
                    log(msg)
                    messages.append(msg)
                    stability_text = new_info["stability_text"]
                    stability_days = new_info["stability_days"]
                else:
                    msg = f"⚠️ 续期失败（Coins 不足或按钮未找到）\n剩余: {stability_text}"
                    warn(msg); messages.append(msg)
            elif stability_days is not None:
                log(f"✅ Stability {fmt_days(stability_days)}，无需续期")
            else:
                warn("未能解析 Stability 时间")

            # ④ 电源状态检查
            power = get_power_status(sb)
            snap(sb, "04-console")

            if power == "running":
                log("✅ 服务器运行中")

            elif power in ("offline", "stopped"):
                log("🔴 服务器离线，自动启动...")
                start_server(sb)
                time.sleep(6)

                final_power = "unknown"
                for i in range(10):
                    final_power = get_power_status(sb)
                    log(f"  等待启动 [{i+1}/10] {final_power}")
                    if final_power in ("running", "starting"):
                        break
                    time.sleep(6)

                snap(sb, "05-after-start")
                msg = (f"🚀 服务器已自动启动！offline → {final_power}"
                       if final_power in ("running", "starting")
                       else f"⚠️ 启动指令已发送，当前: {final_power}")
                log(msg); messages.append(msg)

            elif power in ("starting", "stopping"):
                log(f"⏳ 服务器 {power} 中...")

            else:
                msg = f"❓ 状态未知（{power}），请手动检查"
                warn(msg); messages.append(msg)

            # ⑤ 汇总推送
            if messages:
                stab_info = f"\nStability: {stability_text or '?'}"
                if stability_days:
                    stab_info += f" ({fmt_days(stability_days)})"
                send_tg(
                    f"【Witchly MC 监控】\n" + "\n\n".join(messages) + stab_info,
                    snap(sb, "06-final")
                )
            else:
                log("一切正常，静默退出")

        except Exception as e:
            err(f"异常: {e}")
            send_tg(f"【Witchly 监控】❌ 脚本异常\n{e}", snap(sb, "error"))
            traceback.print_exc()
            sys.exit(1)

    log("▶ 完成")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
