# app.py
import streamlit as st
from pathlib import Path
import shutil
from snapshotper import UltraSnapshotter, ts_name
from utils import zip_dir, slugify

st.set_page_config(page_title="Web Snapshotter Pro", layout="centered")
st.title("🧩 Web Snapshotter Pro (Playwright)")
st.caption("لقطة أوفلاين لصفحات ديناميكية مع تفاعلات ذكية وكاش API اختياري.")

url = st.text_input("ضع الرابط", value="https://felo.ai/en/page/preview/KrcyXakexYzy3cNL2rGJKC?business_type=AGENT_THREAD")

c1, c2 = st.columns(2)
mode = c1.selectbox("وضع الالتقاط", ["static", "interactive", "singlefile"], help="""
static: DOM بعد التنفيذ بدون SPA
interactive: + كاش JSON و fetch patch
singlefile: ملف HTML واحد مكتفٍ (يبني على interactive)
""")
max_pages = c2.slider("عدد الصفحات القصوى (نفس الدومين)", 1, 10, 3)

c3, c4 = st.columns(2)
wait = c3.slider("زمن الانتظار (ms)", 600, 4000, 1600, 100)
scrolls = c4.slider("تمرير Lazy (مرات)", 2, 30, 12, 1)

c5, c6 = st.columns(2)
slow_mo = c5.slider("تبطيء التفاعل (ms)", 0, 300, 0, 10, help="يزود الاستقرار لمواقع حساسة للتوقيت")
follow_sub = c6.checkbox("تتبّع Sub-pages داخل نفس الدومين", True)

job_name = st.text_input("اسم الحفظ (اختياري)")
interactions_file = st.file_uploader("ملف interactions.yaml (اختياري)", type=["yaml","yml"])

btn = st.button("ابدأ الالتقاط", type="primary")

if btn and url.strip():
    # حفظ ملف التخصيص إن تم رفعه
    inter_path = None
    if interactions_file:
        inter_path = Path("interactions.yaml")
        inter_path.write_bytes(interactions_file.read())

    folder = Path(ts_name("snapshot")) if not job_name else Path(slugify(job_name))
    snap = UltraSnapshotter(
        out_dir=folder, mode=mode, wait_idle_ms=wait, scroll_passes=scrolls,
        slow_mo=slow_mo, same_domain_only=True, follow_subpages=follow_sub,
        max_pages=max_pages, interactions_yaml=inter_path
    )
    try:
        with st.spinner("جاري الالتقاط عبر متصفح حقيقي…"):
            result = snap.run(url)
    except Exception as e:
        st.error(f"❌ حدث خطأ أثناء الالتقاط:\n\n{e}")
        st.stop()

    st.success("تم الانتهاء ✅")
    zip_path = zip_dir(result["out_dir"])
    with open(zip_path, "rb") as f:
        st.download_button("⬇️ تنزيل ZIP", f, file_name=Path(zip_path).name)

    if result.get("single_file"):
        p = Path(result["single_file"])
        with open(p, "rb") as f:
            st.download_button("⬇️ تنزيل Single-File HTML", f, file_name=p.name)

    st.info(
        "لو واجهت مشكلة مع file:// شغّل محليًا:\n\n"
        "`python -m http.server 8000`\nثم افتح: `http://localhost:8000/index_offline.html`"
    )
