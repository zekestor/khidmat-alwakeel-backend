import os, re, io, json, sqlite3, secrets, hashlib, shutil
from datetime import datetime
from pathlib import Path

import fitz
from flask import Flask, request, redirect, url_for, render_template, send_file, abort, session, flash
from dotenv import load_dotenv

load_dotenv()

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
PREVIEWS = DATA / "previews"
DB = DATA / "orders.db"

for p in (UPLOADS, OUTPUTS, PREVIEWS):
    p.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-change-me")

PRICE = 5000
PAYMENT_ACCOUNT = "9160453503"

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        service TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        paid_at TEXT,
        original1 TEXT,
        original2 TEXT,
        output1 TEXT,
        output2 TEXT,
        preview1 TEXT,
        preview2 TEXT,
        notes TEXT
    )""")
    conn.commit()
    conn.close()

def safe_name(name):
    name = Path(name or "file.pdf").name
    return re.sub(r"[^A-Za-z0-9._\-\u0600-\u06FF ]+", "_", name)

def extract_pdf_text(path):
    doc = fitz.open(path)
    chunks = []
    for i, page in enumerate(doc):
        chunks.append(f"--- PAGE {i+1} ---\n{page.get_text('text')}")
    doc.close()
    return "\n".join(chunks)

def split_rows(text):
    rows = []
    for line in text.splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if len(s) >= 2 and not re.fullmatch(r"[\d\W_]+", s):
            rows.append(s)
    return rows

def arabic_sort_key(s):
    # Normalize only for sorting; original displayed text is preserved.
    s2 = s.replace("أ","ا").replace("إ","ا").replace("آ","ا").replace("ٱ","ا")
    s2 = s2.replace("ى","ي").replace("ة","ه")
    return s2.casefold()

def make_sorted_pdf(input_path, output_path):
    src = fitz.open(input_path)
    # Preserve the original first-page visual style as much as possible by
    # rebuilding pages from extracted lines in a clean Arabic-friendly layout.
    # Production can replace this renderer with coordinate/table-preserving logic.
    all_rows = []
    for page in src:
        all_rows.extend(split_rows(page.get_text("text")))
    src.close()
    all_rows = sorted(all_rows, key=arabic_sort_key)

    out = fitz.open()
    page = None
    y = 55
    seq = 1
    for row in all_rows:
        if page is None or y > 780:
            page = out.new_page(width=595, height=842)
            page.insert_text((40, 35), "خدمة الوكيل — ترتيب حسب الأبجدية", fontsize=13)
            page.insert_text((40, 50), "تسلسل | البيانات المستخرجة", fontsize=9)
            y = 75
        page.insert_text((40, y), f"{seq} | {row}", fontsize=9)
        y += 15
        seq += 1
    if len(out) == 0:
        page = out.new_page()
        page.insert_text((40, 60), "لم يتم استخراج نص قابل للترتيب من الملف.", fontsize=11)
    out.save(output_path)
    out.close()

def normalize_for_compare(text):
    return [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]

def make_compare_pdf(old_path, new_path, output_path):
    old = set(normalize_for_compare(extract_pdf_text(old_path)))
    new = set(normalize_for_compare(extract_pdf_text(new_path)))
    added = sorted(new-old, key=arabic_sort_key)
    removed = sorted(old-new, key=arabic_sort_key)

    out = fitz.open()
    page = out.new_page(width=595, height=842)
    page.insert_text((40, 40), "خدمة الوكيل — تقرير المتغيرات والمقارنة", fontsize=13)
    y = 70
    page.insert_text((40,y), f"الإضافات المستخرجة: {len(added)}", fontsize=10); y += 20
    for item in added[:45]:
        if y > 800:
            page = out.new_page(width=595, height=842); y = 50
        page.insert_text((40,y), f"+ {item[:100]}", fontsize=8); y += 13
    if y > 770:
        page = out.new_page(width=595, height=842); y = 50
    page.insert_text((40,y), f"الحذف المستخرج: {len(removed)}", fontsize=10); y += 20
    for item in removed[:45]:
        if y > 800:
            page = out.new_page(width=595, height=842); y = 50
        page.insert_text((40,y), f"- {item[:100]}", fontsize=8); y += 13
    out.save(output_path); out.close()

def watermark_first_page(input_pdf, output_pdf):
    doc = fitz.open(input_pdf)
    if len(doc) == 0:
        doc.close(); raise ValueError("PDF فارغ")
    page = doc[0]
    rect = fitz.Rect(0, page.rect.height/2-30, page.rect.width, page.rect.height/2+30)
    page.draw_rect(rect, color=(0.5,0.5,0.5), fill=(1,1,1), fill_opacity=0.72, overlay=True)
    page.insert_textbox(rect, "معاينة فقط — خدمة الوكيل", fontsize=24, color=(0.35,0.35,0.35), align=1, overlay=True)
    preview = fitz.open()
    preview.insert_pdf(doc, from_page=0, to_page=0)
    preview.save(output_pdf)
    preview.close(); doc.close()

def chatgpt_compare(old_text, new_text):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        prompt = """أنت محرك تحليل لملفات وكلاء المواد الغذائية.
قارن بين الملف القديم والجديد. أرجع JSON فقط بالمفاتيح:
additions, deletions, count_changes, total_people, eligible_people, blocked_people, splitting_cases, notes.
لا تخترع أي معلومة غير موجودة. إذا لم تستطع إثبات قيمة، ضع null أو [].
الملف القديم:
""" + old_text[:60000] + "\n\nالملف الجديد:\n" + new_text[:60000]
        r = client.responses.create(model=os.getenv("OPENAI_MODEL","gpt-5.6-luna"),
                                     input=prompt)
        raw = r.output_text
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {"notes": raw}
    except Exception as e:
        return {"error": str(e)}

def order_row(order_id):
    conn=db(); row=conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone(); conn.close()
    return row

@app.route("/")
def index():
    return render_template("index.html", price=PRICE, account=PAYMENT_ACCOUNT)

@app.post("/create-order")
def create_order():
    service=request.form.get("service")
    if service not in ("alphabetical","compare"):
        abort(400)
    f1=request.files.get("pdf1")
    f2=request.files.get("pdf2")
    if not f1 or not f1.filename.lower().endswith(".pdf"):
        flash("ارفع ملف PDF صحيح.")
        return redirect(url_for("index"))
    if service=="compare" and (not f2 or not f2.filename.lower().endswith(".pdf")):
        flash("خدمة المقارنة تحتاج ملفين PDF.")
        return redirect(url_for("index"))

    oid=secrets.token_hex(8)
    d=UPLOADS/oid; d.mkdir()
    p1=d/"old_or_input.pdf"; f1.save(p1)
    p2=None
    if f2:
        p2=d/"new.pdf"; f2.save(p2)

    outdir=OUTPUTS/oid; outdir.mkdir()
    prevdir=PREVIEWS/oid; prevdir.mkdir()
    out1=outdir/"result.pdf"
    prev1=prevdir/"preview.pdf"

    if service=="alphabetical":
        make_sorted_pdf(p1, out1)
        watermark_first_page(out1, prev1)
        out2=prev2=None
    else:
        out2=outdir/"comparison.pdf"; prev2=prevdir/"comparison_preview.pdf"
        make_compare_pdf(p1,p2,out1)
        # Optional second output: ChatGPT structured report if API is configured.
        analysis=chatgpt_compare(extract_pdf_text(p1), extract_pdf_text(p2))
        (outdir/"analysis.json").write_text(json.dumps(analysis or {"status":"local-only"}, ensure_ascii=False, indent=2), encoding="utf-8")
        watermark_first_page(out1, prev1)
        shutil.copy2(out1, out2)
        watermark_first_page(out2, prev2)

    conn=db()
    conn.execute("""INSERT INTO orders
        (id,service,status,created_at,original1,original2,output1,output2,preview1,preview2)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (oid,service,"awaiting_payment",datetime.utcnow().isoformat(),
         str(p1),str(p2) if p2 else None,str(out1),str(out2) if out2 else None,
         str(prev1),str(prev2) if prev2 else None))
    conn.commit(); conn.close()
    return redirect(url_for("order_page", order_id=oid))

@app.route("/order/<order_id>")
def order_page(order_id):
    row=order_row(order_id)
    if not row: abort(404)
    return render_template("order.html", order=row, price=PRICE, account=PAYMENT_ACCOUNT)

@app.post("/order/<order_id>/payment-request")
def payment_request(order_id):
    row=order_row(order_id)
    if not row: abort(404)
    conn=db(); conn.execute("UPDATE orders SET notes=? WHERE id=?", ("تم إرسال إشعار الدفع من الوكيل", order_id)); conn.commit(); conn.close()
    flash("تم إرسال إشعار الدفع. بعد التحقق من التحويل ستتم إتاحة الملف الكامل.")
    return redirect(url_for("order_page", order_id=order_id))

@app.route("/preview/<order_id>/<which>")
def preview(order_id, which):
    row=order_row(order_id)
    if not row: abort(404)
    p=row["preview1"] if which=="1" else row["preview2"]
    if not p: abort(404)
    return send_file(p, mimetype="application/pdf", as_attachment=False)

@app.route("/download/<order_id>/<which>")
def download(order_id, which):
    row=order_row(order_id)
    if not row or row["status"]!="paid":
        abort(403)
    p=row["output1"] if which=="1" else row["output2"]
    if not p: abort(404)
    return send_file(p, mimetype="application/pdf", as_attachment=True, download_name=f"khidmat_alwakeel_{order_id}_{which}.pdf")

def admin_auth():
    return session.get("admin") is True

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method=="POST":
        if request.form.get("username")==os.getenv("ADMIN_USERNAME","admin") and request.form.get("password")==os.getenv("ADMIN_PASSWORD","change-this-password"):
            session["admin"]=True
            return redirect(url_for("admin"))
        flash("بيانات الإدارة غير صحيحة.")
    if not admin_auth():
        return render_template("admin_login.html")
    conn=db(); rows=conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall(); conn.close()
    return render_template("admin.html", orders=rows)

@app.post("/admin/<order_id>/confirm")
def confirm_payment(order_id):
    if not admin_auth(): abort(403)
    conn=db(); conn.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (datetime.utcnow().isoformat(),order_id)); conn.commit(); conn.close()
    return redirect(url_for("admin"))

@app.post("/admin/<order_id>/reject")
def reject_payment(order_id):
    if not admin_auth(): abort(403)
    conn=db(); conn.execute("UPDATE orders SET status='payment_rejected' WHERE id=?", (order_id,)); conn.commit(); conn.close()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=False)
