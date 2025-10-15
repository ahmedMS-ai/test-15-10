import os, sys, subprocess, traceback
import streamlit as st
from pathlib import Path

# محاولة تنزيل المتصفح عند بدء التشغيل (صامتة)
def _ensure_chromium_silent():
    try:
        # تسريع التحميل بين التشغيلات (اختياري)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
        os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

_ensure_chromium_silent()

st.set_page_config(page_title="Web Snapshotter", layout="centered")
st.title("🧩 Web Snapshotter")
st.caption("نسخة أوفلاين ثابتة لصفحات ديناميكية (DOM بعد التنفيذ + أصول محليّة).")

url = st.text_input("ضع الرابط:", value="https://felo.ai/en/page/preview/KrcyXakexYzy3cNL2rGJKC?business_type=AGENT_THREAD")
go = st.button("ابدأ الالتقاط", type="primary")

# “إعدادات متقدمة” (افتراضيًا مخفية) — للمحترفين فقط
with st.expander("إعدادات متقدمة (اختياري)", expanded=False):
    wait_ms = st.slider("زمن الانتظار لكل خطوة (ms)", 800, 4000, 1600, 100)
    max_scrolls = st.slider("تمرير Lazy (أقصى مرات)", 3, 30, 14, 1)

# القيم الافتراضية الذكية لو المستخدم ما فتحش الإكسباندر
wait_ms = locals().get("wait_ms", 1600)
max_scrolls = locals().get("max_scrolls", 14)

def ensure_browser_then(func, *args, **kwargs):
    """يشغّل func، ولو فشل بسبب عدم وجود المتصفح: ينزّل Chromium ويعيد المحاولة مرّة واحدة."""
    from playwright.sync_api import Error as PWError
    tried_install = False
    for attempt in (1, 2):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            needs_install = ("Executable doesn't exist" in msg) or ("playwright install" in msg)
            if needs_install and not tried_install:
                tried_install = True
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
                continue
            raise

if go and url.strip():
    try:
        # import هنا لتقليل وقت الإقلاع
        from snapshotper import StaticSnapshotter, ts_name
        out_dir = Path(ts_name("snapshot"))

        def _run():
            snap = StaticSnapshotter(out_dir=out_dir, wait_idle_ms=wait_ms, scroll_passes=max_scrolls)
            return snap.run(url)

        with st.spinner("جاري الالتقاط عبر متصفح حقيقي…"):
            folder = ensure_browser_then(_run)

        st.success("تم الانتهاء ✅")
        # ضغط المجلد وإتاحة التحميل
        from utils import zip_dir
        zip_path = zip_dir(folder)
        with open(zip_path, "rb") as f:
            st.download_button("⬇️ تنزيل ZIP", f, file_name=Path(zip_path).name)

        st.info("لو واجهت مشكلة مع file:// شغّل محليًا:\n`python -m http.server 8000` ثم افتح `http://localhost:8000/index_offline.html`")

    except Exception as e:
        st.error("❌ حدث خطأ أثناء الالتقاط:\n" + "".join(traceback.format_exception_only(type(e), e)))
