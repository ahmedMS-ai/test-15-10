# Web Snapshotter Pro (Playwright)

أداة مفتوحة المصدر لالتقاط نسخة أوفلاين لصفحات الويب الديناميكية (حتى SPA/Next/React) عبر:
- DOM بعد التنفيذ
- حفظ الأصول محليًا (CSS/JS/صور/خطوط)
- تفاعلات ذكية (scroll، theme toggle، anchors، modals)
- كاش API JSON اختياري + fetch/XHR patch
- إنتاج ملف HTML واحد مكتفٍ (اختياري)

## الاستخدام
### محلي
```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
streamlit run app.py
