# utils.py
from pathlib import Path
import shutil, re, unicodedata, time

def zip_dir(src: str|Path) -> str:
    src = str(src)
    return shutil.make_archive(src, "zip", src)

def slugify(value:str)->str:
    value = unicodedata.normalize('NFKD', value).encode('ascii','ignore').decode('ascii')
    value = re.sub(r'[^a-zA-Z0-9_-]+','-', value).strip('-').lower()
    return value or f"job-{int(time.time())}"
