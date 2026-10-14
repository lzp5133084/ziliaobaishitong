# -*- coding: utf-8 -*-
"""
资料百事通 Demo V0.1 —— 七项验收自动化测试
作者：军哥懂保（微信：xunijiayuan）

测试使用独立的临时数据库与图片目录，不影响正式数据。
运行：python tests.py
"""
import io
import os
import shutil
import sys
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from PIL import Image, ImageDraw

# ---------- 准备临时环境 ----------
TMP = tempfile.mkdtemp(prefix="zlBST_test_")
app_module.DB_PATH = os.path.join(TMP, "test.db")
app_module.UPLOAD_DIR = os.path.join(TMP, "uploads")
os.makedirs(app_module.UPLOAD_DIR, exist_ok=True)
app_module.init_db()

flask_app = app_module.app
flask_app.config["TESTING"] = True
client_factory = lambda: flask_app.test_client()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [通过] %s" % name)
    else:
        FAIL.append(name)
        print("  [失败] %s  %s" % (name, detail))


def make_png(idx=1):
    """生成模拟文档样式的测试PNG图片（图形代替文字，避免字体问题）"""
    img = Image.new("RGB", (640, 420), "#e8f2ff")
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 620, 400], outline="#2b7fff", width=4)
    d.rectangle([40, 50, 300, 90], fill="#2b7fff")            # 标题条
    for i in range(6):                                        # 内容行
        d.rectangle([40, 120 + i * 34, 560 - (i % 3) * 60, 142 + i * 34], fill="#c7dcf7")
    d.ellipse([500, 40, 590, 130], outline="#22b573", width=5)  # 印章圈
    d.rectangle([40, 350, 240, 390], fill="#ff9d2e")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


DOC_FORM = {
    "industry_id": "1", "province": "四川省", "city": "泸州市", "county": "江阳区",
    "org_name": "泸州市XX医院", "source_org": "西南医科大学附属医院", "title": "出院记录样例-测试一", "business": "住院医疗理赔",
    "doc_type": "出院记录", "tags": "出院记录,理赔,住院资料", "purpose": "用于向保险公司申请住院医疗理赔",
    "obtain_method": "出院后向医院病案室申请打印", "notes": "不同医院格式略有差异，仅供形状参考",
    "source": "本人2025年住院时取得", "valid_period": "2025年版", "privacy_confirmed": "1",
}


def form_with_files(form, files):
    """werkzeug测试客户端：同名字段多文件需以列表方式传入"""
    data = dict(form)
    imgs = []
    for name, (buf, fn) in files:
        imgs.append((buf, fn))
    data["images"] = imgs
    return data


def register_login(username, nickname=""):
    c = client_factory()
    c.post("/register", data={"username": username, "password": "test123456",
                              "password2": "test123456", "nickname": nickname or username,
                              "region": "四川省泸州市"}, follow_redirects=True)
    return c


def db_q(sql, args=()):
    db = sqlite3.connect(app_module.DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


print("=" * 62)
print("资料百事通 Demo V0.1 验收测试（七项）")
print("=" * 62)

# ---------------- 测试一：上传 ----------------
print("\n【测试一】用户上传：填写四川泸州某医院出院记录 + 2张脱敏图片")
cA = register_login("userA", "测试用户A")
files = [("images", (make_png("出院记录 第1页"), "chuangyuan_p1.png")),
         ("images", (make_png("出院记录 第2页"), "chuangyuan_p2.png"))]
data = form_with_files(DOC_FORM, files)
r = cA.post("/upload", data=data, follow_redirects=True)
rows = db_q("SELECT * FROM documents WHERE title=?", ("出院记录样例-测试一",))
check("上传成功且数据库产生记录", r.status_code == 200 and len(rows) == 1)
if rows:
    doc1 = rows[0]
    check("初始状态为待审核", doc1["status"] == "pending", doc1["status"])
    imgs = db_q("SELECT * FROM document_images WHERE document_id=?", (doc1["id"],))
    check("2张图片已保存到服务器", len(imgs) == 2 and
          all(os.path.exists(os.path.join(app_module.UPLOAD_DIR, i["filename"])) for i in imgs),
          "图片数=%d" % len(imgs))
    check("图片经Pillow重编码（EXIF已清除）",
          all(Image.open(os.path.join(app_module.UPLOAD_DIR, i["filename"])).info.get("exif") is None
              for i in imgs))
    check("操作日志已记录上传", len(db_q(
        "SELECT * FROM audit_logs WHERE document_id=? AND action='上传资料'", (doc1["id"],))) == 1)
    # 上传页失败场景：缺图片
    bad = dict(DOC_FORM); bad.pop("privacy_confirmed")
    r2 = cA.post("/upload", data=bad)
    check("缺少隐私确认时拒绝提交(400)", r2.status_code == 400, str(r2.status_code))

# ---------------- 测试二：审核 ----------------
print("\n【测试二】管理员审核：待审核列表可见 → 通过后才公开")
cAdmin = client_factory()
cAdmin.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
# 未审核前，公开搜索不应找到
cB = register_login("userB", "测试用户B")
r_pre = cB.get("/search", query_string={"kw": "出院记录样例-测试一"})
check("未审核资料不出现在公开搜索", "doc-card" not in r_pre.get_data(as_text=True),
      "结果页出现了资料卡片")
# 普通用户不能访问管理后台与审核接口
check("普通用户访问后台被拒绝(403)", cB.get("/admin").status_code == 403)
check("普通用户调用审核接口被拒绝(403)",
      cB.post("/admin/review/%d" % (doc1["id"] if rows else 1),
              data={"action": "approve"}).status_code == 403)
# 管理员查看并审核
r_admin = cAdmin.get("/admin")
check("管理员可见待审核列表", r_admin.status_code == 200 and "出院记录样例-测试一" in r_admin.get_data(as_text=True))
r_ok = cAdmin.post("/admin/review/%d" % doc1["id"], data={"action": "approve"}, follow_redirects=True)
st = db_q("SELECT status FROM documents WHERE id=?", (doc1["id"],))[0]["status"]
check("审核通过后状态变为已发布", st == "published", st)
# 驳回必须填原因（此时doc1已发布，空原因驳回应被拒绝且状态不变）
r_rej = cAdmin.post("/admin/review/%d" % doc1["id"],
                    data={"action": "reject", "reason": ""}, follow_redirects=True)
st_after = db_q("SELECT status FROM documents WHERE id=?", (doc1["id"],))[0]["status"]
check("驳回未填原因被拒绝", "驳回时必须填写" in r_rej.get_data(as_text=True) and st_after == "published",
      "状态=%s" % st_after)

# ---------------- 测试三：搜索 ----------------
print("\n【测试三】用户B搜索：关键词+地区组合命中资料A")
r_s = cB.get("/search", query_string={"kw": "出院记录", "province": "四川省", "city": "泸州市"})
body = r_s.get_data(as_text=True)
check("关键词+地区组合搜索命中", r_s.status_code == 200 and "出院记录样例-测试一" in body and "共找到" in body)
r_kw = cB.get("/search", query_string={"kw": "理赔"})
check("标签关键词(理赔)可命中", "出院记录样例-测试一" in r_kw.get_data(as_text=True))
r_api = cB.get("/api/search", query_string={"kw": "出院记录"})
check("API搜索接口返回JSON", r_api.status_code == 200 and b'"total"' in r_api.data)

# ---------------- 测试四：查看详情 ----------------
print("\n【测试四】用户B打开详情页：查看图片与说明")
r_d = cB.get("/doc/%d" % doc1["id"])
body_d = r_d.get_data(as_text=True)
check("详情页正常打开", r_d.status_code == 200)
check("展示文字说明/机构/作者", all(k in body_d for k in ("泸州市XX医院", "测试用户A", "病案室", "2025年版")))
img1 = db_q("SELECT filename FROM document_images WHERE document_id=?", (doc1["id"],))[0]["filename"]
r_img = cB.get("/uploads/%s" % img1)
check("图片文件可真实访问", r_img.status_code == 200 and r_img.data[:4] != b"")
check("浏览次数+1", db_q("SELECT views FROM documents WHERE id=?", (doc1["id"],))[0]["views"] >= 1)

# ---------------- 测试五：数据持久化 ----------------
print("\n【测试五】数据持久化：重启后数据库记录仍在")
raw = sqlite3.connect(app_module.DB_PATH)
cnt = raw.execute("SELECT COUNT(*) FROM documents WHERE title='出院记录样例-测试一'").fetchone()[0]
raw.close()
app_module.init_db()  # 模拟服务重启后再次初始化（不应破坏数据）
cnt2 = db_q("SELECT COUNT(*) c FROM documents WHERE title='出院记录样例-测试一'")[0]["c"]
check("重启后记录仍然存在", cnt == 1 and cnt2 == 1, "%s/%s" % (cnt, cnt2))

# ---------------- 测试六：权限 ----------------
print("\n【测试六】权限隔离：不能改他人资料、未审核资料他人不可见")
d2 = dict(DOC_FORM)
d2["title"] = "待审核资料-权限测试"
d2["images"] = [(make_png(), "pending.png")]
cA.post("/upload", data=d2)
doc2 = db_q("SELECT * FROM documents WHERE title=?", ("待审核资料-权限测试",))[0]
check("他人未审核资料不可见(404)", cB.get("/doc/%d" % doc2["id"]).status_code == 404)
check("他人不能修改资料(403)", cB.get("/my/edit/%d" % doc1["id"]).status_code == 403 and
      cB.post("/my/edit/%d" % doc1["id"], data=dict(DOC_FORM)).status_code == 403)
check("他人不能删除资料(403)", cB.post("/my/delete/%d" % doc1["id"]).status_code == 403)
check("未登录上传被拦截", client_factory().get("/upload").status_code == 302)
check("API未审核资料不返回数据", cB.get("/api/doc/%d" % doc2["id"]).status_code == 404)
# 修改自己被驳回的资料后重新进入待审核
cAdmin.post("/admin/review/%d" % doc2["id"], data={"action": "reject", "reason": "图片模糊请重新上传"})
edit = dict(DOC_FORM); edit["title"] = "待审核资料-权限测试-已修改"; edit.pop("images", None); edit.pop("privacy_confirmed", None)
r_e = cA.post("/my/edit/%d" % doc2["id"], data=edit, follow_redirects=True)
st2 = db_q("SELECT status,reject_reason FROM documents WHERE id=?", (doc2["id"],))[0]
check("修改后资料重新进入待审核", r_e.status_code == 200 and st2["status"] == "pending"
      and st2["reject_reason"] == "", st2["status"])

# ---------------- 测试七：空结果 ----------------
print("\n【测试七】无结果：正确显示空结果页面，不伪造数据")
r_n = cB.get("/search", query_string={"kw": "绝对不存在的资料XYZ123"})
body_n = r_n.get_data(as_text=True)
check("空结果显示无结果提示", r_n.status_code == 200 and "没有找到符合条件的资料" in body_n)
check("空结果不出现任何资料卡片", "doc-card" not in body_n)

# ==================== V1.0 新增功能测试 ====================
print("\n" + "=" * 62)
print("V1.0 新增功能测试")
print("=" * 62)

# ---------------- 测试八：分类管理 ----------------
print("\n【测试八】分类管理：管理员可维护行业/机构/业务/资料类型")
# 检查分类API
r_cat = cB.get("/api/categories/tree")
check("分类级联API返回JSON", r_cat.status_code == 200 and b'"ok":true' in r_cat.data)
cat_data = r_cat.get_json()
check("分类API包含行业数据", len(cat_data["items"]) >= 1)
check("分类API包含机构/业务/资料类型",
      len(cat_data["items"][0]["orgs"]) > 0 and
      len(cat_data["items"][0]["businesses"]) > 0 and
      len(cat_data["items"][0]["doc_types"]) > 0)
# 管理员访问分类管理
r_admin_cat = cAdmin.get("/admin/categories")
check("管理员可访问分类管理", r_admin_cat.status_code == 200)
# 普通用户不能访问分类管理
check("普通用户访问分类管理被拒绝(403)", cB.get("/admin/categories").status_code == 403)
# 添加新分类
cAdmin.post("/admin/categories/add", data={
    "parent_id": "0", "type": "industry", "name": "测试行业V1", "sort": "99"
}, follow_redirects=True)
new_ind = db_q("SELECT * FROM categories WHERE name='测试行业V1' AND type='industry'")
check("管理员可添加新行业", len(new_ind) == 1)
# 添加子分类
cAdmin.post("/admin/categories/add", data={
    "parent_id": str(new_ind[0]["id"]), "type": "org", "name": "测试机构A", "sort": "1"
}, follow_redirects=True)
new_org = db_q("SELECT * FROM categories WHERE name='测试机构A' AND type='org'")
check("管理员可添加子分类(机构)", len(new_org) == 1)

# ---------------- 测试九：版本管理 ----------------
print("\n【测试九】版本管理：用户可为已发布资料提交新版本")
# 确保doc1已发布
st = db_q("SELECT status FROM documents WHERE id=?", (doc1["id"],))[0]["status"]
check("资料当前状态为已发布", st == "published")
# 用户A提交新版本
new_ver_form = dict(DOC_FORM)
new_ver_form["title"] = "出院记录样例-测试一-V2"
new_ver_form["images"] = [(make_png("V2版本"), "v2_p1.png")]
r_nv = cA.post("/doc/%d/new_version" % doc1["id"], data=new_ver_form, follow_redirects=True)
check("提交新版本成功", r_nv.status_code == 200 and "新版本已提交审核" in r_nv.get_data(as_text=True))
# 检查版本表
vers = db_q("SELECT * FROM document_versions WHERE document_id=? ORDER BY version_no DESC", (doc1["id"],))
check("版本表产生新记录", len(vers) == 1)
# 新版本号应为2（原始版本为v1）
actual_ver = vers[0]["version_no"]
check("新版本号为2", actual_ver == 2, "实际版本号=%s" % actual_ver)
check("新版本状态为待审核", vers[0]["status"] == "pending")
# 版本历史页面
r_vh = cB.get("/doc/%d/versions" % doc1["id"])
check("版本历史页面可访问", r_vh.status_code == 200)
check("版本历史显示当前版本和新版本", "v1" in r_vh.get_data(as_text=True) and "v2" in r_vh.get_data(as_text=True))
# 版本详情页面（待审核版本仅作者和管理员可见）
r_vd_author = cA.get("/version/%d" % vers[0]["id"])
check("作者可访问待审核版本详情", r_vd_author.status_code == 200)
r_vd_other = cB.get("/version/%d" % vers[0]["id"])
check("他人访问待审核版本被拒绝(404)", r_vd_other.status_code == 404)
r_vd_admin = cAdmin.get("/version/%d" % vers[0]["id"])
check("管理员可访问待审核版本详情", r_vd_admin.status_code == 200)

# ---------------- 测试十：纠错反馈 ----------------
print("\n【测试十】纠错反馈：用户可提交有帮助/内容有误/已过时反馈")
# 用户B提交有帮助反馈
r_fb1 = cB.post("/doc/%d/feedback" % doc1["id"], data={"type": "helpful"}, follow_redirects=True)
check("提交有帮助反馈成功", r_fb1.status_code == 200)
fb1 = db_q("SELECT * FROM feedback WHERE document_id=? AND type='helpful'", (doc1["id"],))
check("有帮助反馈已入库", len(fb1) == 1 and fb1[0]["status"] == "pending")
# 重复提交同类型反馈
r_fb2 = cB.post("/doc/%d/feedback" % doc1["id"], data={"type": "helpful"}, follow_redirects=True)
check("重复提交同类型反馈被拦截", "已提交过" in r_fb2.get_data(as_text=True))
# 提交内容有误反馈（需填理由）
r_fb3 = cB.post("/doc/%d/feedback" % doc1["id"], data={"type": "error", "reason": "机构名称有误"}, follow_redirects=True)
check("提交内容有误反馈成功", r_fb3.status_code == 200)
# 提交内容有误但不填理由
r_fb4 = cB.post("/doc/%d/feedback" % doc1["id"], data={"type": "error"}, follow_redirects=True)
check("内容有误不填理由被拒绝", "请填写反馈理由" in r_fb4.get_data(as_text=True))
# 管理员查看反馈
r_admin_fb = cAdmin.get("/admin/feedback")
check("管理员可访问反馈管理", r_admin_fb.status_code == 200)
check("反馈管理显示待处理反馈", "内容有误" in r_admin_fb.get_data(as_text=True) or "已过时" in r_admin_fb.get_data(as_text=True))
# 管理员处理反馈
cAdmin.post("/admin/feedback/%d" % fb1[0]["id"], data={"action": "process", "admin_note": "已确认"}, follow_redirects=True)
fb1_after = db_q("SELECT status FROM feedback WHERE id=?", (fb1[0]["id"],))[0]
check("管理员处理反馈后状态更新", fb1_after["status"] == "processed")

# ---------------- 测试十一：作者主页 ----------------
print("\n【测试十一】作者主页：展示公开信息和贡献统计")
r_author = cB.get("/author/%d" % doc1["user_id"])
body_author = r_author.get_data(as_text=True)
check("作者主页可访问", r_author.status_code == 200)
check("作者主页显示昵称", "测试用户A" in body_author)
check("作者主页显示已发布资料", "出院记录样例-测试一" in body_author)
check("作者主页显示贡献统计", "发布资料" in body_author and "总浏览" in body_author)

# ---------------- 测试十二：用户管理 ----------------
print("\n【测试十二】用户管理：管理员可管理用户状态和角色")
r_users = cAdmin.get("/admin/users")
check("管理员可访问用户管理", r_users.status_code == 200)
# 禁用用户
cAdmin.post("/admin/users/%d" % doc1["user_id"], data={"action": "toggle_status"}, follow_redirects=True)
u_status = db_q("SELECT status FROM users WHERE id=?", (doc1["user_id"],))[0]["status"]
check("管理员可禁用用户", u_status == 0)
# 重新启用
cAdmin.post("/admin/users/%d" % doc1["user_id"], data={"action": "toggle_status"}, follow_redirects=True)
u_status2 = db_q("SELECT status FROM users WHERE id=?", (doc1["user_id"],))[0]["status"]
check("管理员可重新启用用户", u_status2 == 1)
# 不能修改admin角色
r_admin_role = cAdmin.post("/admin/users/1", data={"action": "toggle_role"}, follow_redirects=True)
check("不能修改内置管理员角色", "不能修改内置管理员" in r_admin_role.get_data(as_text=True))

# ---------------- V1.5 AI智能搜索测试 ----------------
print("\n" + "=" * 62)
print("V1.5 AI智能搜索测试")
print("=" * 62)

# 检查AI服务模块导入
import ai_service
check("AI服务模块可导入", ai_service is not None)

# AI可用性检查（无API Key时降级）
ai_available = ai_service.is_ai_available()
check("AI可用性检查函数正常", isinstance(ai_available, bool))
print("  [信息] AI服务当前状态：%s" % ("已配置" if ai_available else "未配置API Key，使用降级模式"))

# 降级模式解析测试
parsed = ai_service.parse_search_intent("四川泸州平安人寿出院记录")
check("降级解析返回字典", isinstance(parsed, dict))
check("降级解析包含省份", parsed.get("province") == "四川省")
check("降级解析包含城市", parsed.get("city") == "泸州市")
check("降级解析包含机构关键词", "平安" in parsed.get("org_name", ""))

# 降级模式无结果回复
no_result_resp = ai_service.generate_fallback_no_result(parsed)
check("无结果降级回复生成", isinstance(no_result_resp, str) and len(no_result_resp) > 10)

# 降级模式有结果回复
fake_docs = [
    {"id": 1, "title": "出院记录样例", "province": "四川省", "city": "泸州市",
     "org_name": "平安人寿", "doc_type": "出院记录", "updated_at": "2026-01-01 10:00:00"}
]
result_resp = ai_service.generate_fallback_result(parsed, fake_docs, 1)
check("有结果降级回复生成", isinstance(result_resp, str) and "出院记录样例" in result_resp)

# AI搜索API接口测试（降级模式）
r_ai = cB.get("/api/ai/search", query_string={"q": "四川泸州出院记录"})
check("AI搜索API返回JSON", r_ai.status_code == 200)
ai_data = r_ai.get_json()
check("AI搜索API包含必要字段", all(k in ai_data for k in ["ok", "answer", "parsed", "docs", "total"]))
check("AI搜索API解析出省份", ai_data["parsed"].get("province") == "四川省")
check("AI搜索API返回资料列表", isinstance(ai_data["docs"], list))

# 空查询测试
r_ai_empty = cB.get("/api/ai/search", query_string={"q": ""})
check("AI搜索空查询返回400", r_ai_empty.status_code == 400)

# ==================== V2.0 社区共创测试 ====================
print("\n" + "=" * 62)
print("V2.0 社区共创测试")
print("=" * 62)

# ---------------- 测试十三：社区讨论 ----------------
print("\n【测试十三】社区讨论：发表/回复/展示")
# 未登录发表被拦截
r_dis0 = client_factory().post("/doc/%d/discuss" % doc1["id"],
                               data={"content": "未登录测试内容"})
check("未登录发表讨论被拦截(302)", r_dis0.status_code == 302)
# 用户B发表讨论
r_dis1 = cB.post("/doc/%d/discuss" % doc1["id"],
                 data={"content": "请问这个出院记录需要医院盖章吗？理赔时被要求补盖章。"},
                 follow_redirects=True)
check("用户发表讨论成功", r_dis1.status_code == 200 and "发表成功" in r_dis1.get_data(as_text=True))
dis1 = db_q("SELECT * FROM discussions WHERE document_id=? AND parent_id=0", (doc1["id"],))
check("讨论已入库且状态正常", len(dis1) == 1 and dis1[0]["status"] == "published")
# 详情页展示讨论
r_d2 = cB.get("/doc/%d" % doc1["id"])
check("详情页展示讨论内容", "医院盖章" in r_d2.get_data(as_text=True))
# 内容过短被拒绝
r_dis2 = cB.post("/doc/%d/discuss" % doc1["id"], data={"content": "好的"}, follow_redirects=True)
check("内容过短被拒绝(<5字)", "至少5个字" in r_dis2.get_data(as_text=True))
# 用户A回复讨论
r_dis3 = cA.post("/doc/%d/discuss" % doc1["id"],
                 data={"content": "需要的，病案室打印后直接盖章才有效。",
                       "parent_id": str(dis1[0]["id"])},
                 follow_redirects=True)
dis2 = db_q("SELECT * FROM discussions WHERE parent_id=?", (dis1[0]["id"],))
check("回复讨论成功且关联正确", r_dis3.status_code == 200 and len(dis2) == 1)
# 发表讨论获得积分
pl = db_q("SELECT * FROM points_log WHERE user_id=? AND action='discussion'", (dis1[0]["user_id"],))
check("发表讨论获得积分(+1)", len(pl) >= 1 and pl[0]["points"] == 1)

# ---------------- 测试十四：发言额度 ----------------
print("\n【测试十四】发言额度：Lv1每日5条，超限拦截，管理员不限")
cC = register_login("userC", "测试用户C")
quota_ok = True
for i in range(5):
    r_q = cC.post("/doc/%d/discuss" % doc1["id"],
                  data={"content": "额度测试发言第%d条，内容足够长。" % (i + 1)},
                  follow_redirects=True)
    if "发表成功" not in r_q.get_data(as_text=True):
        quota_ok = False
        break
check("Lv1用户前5条发言成功", quota_ok)
r_q6 = cC.post("/doc/%d/discuss" % doc1["id"],
               data={"content": "第六条发言应被额度拦截。"}, follow_redirects=True)
check("Lv1用户第6条发言被拦截", "额度已用完" in r_q6.get_data(as_text=True))
# 管理员不限额度
r_qa = cAdmin.post("/doc/%d/discuss" % doc1["id"],
                   data={"content": "管理员发言不受额度限制。"}, follow_redirects=True)
check("管理员发言不受额度限制", "发表成功" in r_qa.get_data(as_text=True))

# ---------------- 测试十五：贡献等级 ----------------
print("\n【测试十五】贡献等级：积分累计与等级计算")
# userA：审核通过10 + 新版本5 + 回复讨论1 = 16分 → Lv2 热心人
pts_a = db_q("SELECT COALESCE(SUM(points),0) s FROM points_log WHERE user_id=?", (doc1["user_id"],))[0]["s"]
check("审核通过/新版本/讨论积分已累计", pts_a == 16, "实际积分=%s" % pts_a)
with flask_app.test_request_context():
    import flask
    flask.g.db = sqlite3.connect(app_module.DB_PATH)
    flask.g.db.row_factory = sqlite3.Row
    lv_a = app_module.user_level(doc1["user_id"])
    lv_b = app_module.user_level(dis1[0]["user_id"])
check("16分为Lv2热心人且额度10条", lv_a["level_name"] == "Lv2 热心人" and lv_a["daily_quota"] == 10,
      "%s/%s" % (lv_a["level_name"], lv_a["daily_quota"]))
check("3分仍为Lv1新手", lv_b["level_name"] == "Lv1 新手", lv_b["level_name"])
# 作者主页显示等级徽章
r_author2 = cB.get("/author/%d" % doc1["user_id"])
check("作者主页显示等级徽章", "Lv2 热心人" in r_author2.get_data(as_text=True))

# ---------------- 测试十六：讨论管理与AI整理 ----------------
print("\n【测试十六】讨论管理：删除权限/管理员隐藏 + AI高频问题整理")
# 他人不能删除讨论(403)
r_del0 = cC.post("/discuss/%d/delete" % dis1[0]["id"])
check("他人不能删除讨论(403)", r_del0.status_code == 403)
# 本人可删除（先删B的主讨论会连带删A的回复，改删C自己的一条）
c_own = db_q("SELECT * FROM discussions WHERE user_id=? LIMIT 1",
             (db_q("SELECT id FROM users WHERE username='userC'")[0]["id"],))[0]
r_del1 = cC.post("/discuss/%d/delete" % c_own["id"], follow_redirects=True)
check("本人可删除自己的讨论", r_del1.status_code == 200 and
      len(db_q("SELECT * FROM discussions WHERE id=?", (c_own["id"],))) == 0)
# 管理员隐藏A的回复
r_hide = cAdmin.post("/admin/discuss/%d/hide" % dis2[0]["id"], follow_redirects=True)
check("管理员可隐藏讨论", r_hide.status_code == 200 and
      db_q("SELECT status FROM discussions WHERE id=?", (dis2[0]["id"],))[0]["status"] == "hidden")
# 隐藏后普通用户不可见
r_d3 = cB.get("/doc/%d" % doc1["id"])
check("隐藏讨论对普通用户不可见", "病案室打印后直接盖章" not in r_d3.get_data(as_text=True))
# 管理员仍可见（带隐藏标记）
r_d4 = cAdmin.get("/doc/%d" % doc1["id"])
check("管理员可见隐藏讨论及标记", "已隐藏" in r_d4.get_data(as_text=True))
# AI高频问题整理API
r_digest = cB.get("/api/ai/community_digest")
digest = r_digest.get_json()
check("社区整理API返回JSON", r_digest.status_code == 200 and digest["ok"])
check("社区整理包含摘要", isinstance(digest["summary"], str) and len(digest["summary"]) > 5)
check("社区整理统计讨论数", digest["total_discussions"] >= 5, str(digest["total_discussions"]))
# 空讨论场景
empty_digest = ai_service.summarize_community([])
check("空讨论时整理正常返回", empty_digest["ok"] and empty_digest["total_discussions"] == 0)

# ==================== V3.0 商业化功能测试 ====================
print("\n" + "=" * 62)
print("V3.0 商业化功能测试（会员/广告激励/付费咨询/需求悬赏）")
print("=" * 62)
import zipfile


def give_points(username, pts):
    """测试辅助：直接给指定用户充值积分"""
    uid = db_q("SELECT id FROM users WHERE username=?", (username,))[0]["id"]
    db = sqlite3.connect(app_module.DB_PATH)
    db.execute("INSERT INTO points_log(user_id,points,action,detail,created_at) VALUES(?,?,?,?,?)",
               (uid, pts, "test_grant", "测试充值", "2026-01-01 00:00:00"))
    db.commit()
    db.close()
    return uid


def points_of(username):
    uid = db_q("SELECT id FROM users WHERE username=?", (username,))[0]["id"]
    return db_q("SELECT COALESCE(SUM(points),0) s FROM points_log WHERE user_id=?", (uid,))[0]["s"]


# ---------------- 测试十七：VIP会员 ----------------
print("\n【测试十七】VIP会员：兑换/权益/优先审核/管理员开通")
# 1. 积分不足兑换失败（userB 当前积分远不足200）
r_vip0 = cB.post("/vip/redeem", data={"days": "30"}, follow_redirects=True)
check("积分不足兑换会员被拒绝", "积分不足" in r_vip0.get_data(as_text=True))
check("兑换失败未产生会员记录",
      len(db_q("SELECT * FROM membership WHERE user_id=(SELECT id FROM users WHERE username='userB')")) == 0)
# 2. 充值后兑换成功，积分正确扣减
pts_c0 = points_of("userC")
give_points("userC", 300)
r_vip1 = cC.post("/vip/redeem", data={"days": "30"}, follow_redirects=True)
ms = db_q("SELECT * FROM membership WHERE user_id=(SELECT id FROM users WHERE username='userC')")
check("兑换成功产生会员记录", r_vip1.status_code == 200 and len(ms) == 1 and "开通成功" in r_vip1.get_data(as_text=True))
check("兑换后积分正确扣减(-200)", points_of("userC") == pts_c0 + 300 - 200, "当前=%s" % points_of("userC"))
check("会员有效期在未来", ms[0]["expire_at"] > "2026-10-13", ms[0]["expire_at"])
# 3. VIP状态API
r_vs = cC.get("/api/vip/status")
vs = r_vs.get_json()
check("VIP状态API返回is_vip=true", r_vs.status_code == 200 and vs["ok"] and vs["is_vip"])
check("未登录VIP状态API返回401", client_factory().get("/api/vip/status").status_code == 401)
# 4. VIP高级筛选：VIP可见排序入口，非VIP不可见且参数被忽略
r_sv = cC.get("/search", query_string={"sort": "views"})
r_sb = cB.get("/search", query_string={"sort": "views"})
check("VIP搜索页显示高级排序", "排序（VIP权益）" in r_sv.get_data(as_text=True))
check("非VIP搜索页不显示高级排序", "排序（VIP权益）" not in r_sb.get_data(as_text=True))
# 5. 打包下载：非VIP被拒，VIP返回ZIP
r_dl0 = cB.get("/doc/%d/download" % doc1["id"])
check("非VIP打包下载被拒绝(重定向会员页)", r_dl0.status_code == 302 and "/vip" in r_dl0.headers.get("Location", ""))
r_dl1 = cC.get("/doc/%d/download" % doc1["id"])
check("VIP打包下载返回ZIP", r_dl1.status_code == 200 and "zip" in r_dl1.headers.get("Content-Type", ""))
zf = zipfile.ZipFile(io.BytesIO(r_dl1.data))
check("ZIP内含全部2张图片", len(zf.namelist()) == 2, str(zf.namelist()))
# 6. VIP优先审核：非VIP先传、VIP后传，后台列表VIP在前
d_v0 = dict(DOC_FORM); d_v0["title"] = "普通用户审核测试资料"; d_v0["images"] = [(make_png(), "nv.png")]
cB.post("/upload", data=d_v0)
d_v1 = dict(DOC_FORM); d_v1["title"] = "VIP优先审核测试资料"; d_v1["images"] = [(make_png(), "vv.png")]
cC.post("/upload", data=d_v1)
r_adm = cAdmin.get("/admin")
body_adm = r_adm.get_data(as_text=True)
check("后台待审核VIP资料排在前面",
      body_adm.find("VIP优先审核测试资料") != -1 and
      body_adm.find("VIP优先审核测试资料") < body_adm.find("普通用户审核测试资料"))
check("后台显示VIP优先标识", "VIP优先" in body_adm)
# 7. 管理员手动开通会员
uid_b = db_q("SELECT id FROM users WHERE username='userB'")[0]["id"]
r_gv = cAdmin.post("/admin/users/%d/vip" % uid_b, data={"days": "30"}, follow_redirects=True)
check("管理员可为用户开通会员", r_gv.status_code == 200 and "开通" in r_gv.get_data(as_text=True))
check("开通后userB为VIP", cB.get("/api/vip/status").get_json()["is_vip"])
# 清理：把两条优先审核测试资料审核通过/驳回，避免影响后续
doc_nv = db_q("SELECT * FROM documents WHERE title='普通用户审核测试资料'")[0]
doc_vv = db_q("SELECT * FROM documents WHERE title='VIP优先审核测试资料'")[0]
cAdmin.post("/admin/review/%d" % doc_nv["id"], data={"action": "approve"})
cAdmin.post("/admin/review/%d" % doc_vv["id"], data={"action": "approve"})

# ---------------- 测试十八：广告激励 ----------------
print("\n【测试十八】广告激励：每日上限3次，每次+5积分，防刷")
check("未登录广告页被拦截(302)", client_factory().get("/ad").status_code == 302)
pts_c1 = points_of("userC")
ok_ad = True
for i in range(3):
    r_ad = cC.post("/ad/claim", follow_redirects=True)
    if "已到账" not in r_ad.get_data(as_text=True):
        ok_ad = False
        break
check("每日前3次领取成功", ok_ad)
check("3次共+15积分", points_of("userC") == pts_c1 + 15, "变动=%s" % (points_of("userC") - pts_c1))
uid_c = db_q("SELECT id FROM users WHERE username='userC'")[0]["id"]
check("ad_rewards表记录3条", len(db_q("SELECT * FROM ad_rewards WHERE user_id=?", (uid_c,))) == 3)
r_ad4 = cC.post("/ad/claim", follow_redirects=True)
check("第4次被每日上限拦截", "已达上限" in r_ad4.get_data(as_text=True))
check("拦截后积分未变", points_of("userC") == pts_c1 + 15)

# ---------------- 测试十九：作者付费咨询 ----------------
print("\n【测试十九】付费咨询：积分支付→作者回答结算→未回答可退款")
cD = register_login("userD", "测试用户D")
# 1. 积分不足(0分)咨询被拒
r_cs0 = cD.post("/doc/%d/consult" % doc1["id"], data={"question": "请问需要提前预约吗？"},
                follow_redirects=True)
check("积分不足咨询被拒绝", "积分不足" in r_cs0.get_data(as_text=True))
# 2. 充值后咨询成功扣费
give_points("userD", 50)
r_cs1 = cD.post("/doc/%d/consult" % doc1["id"], data={"question": "请问需要提前预约吗？周末能办理吗？"},
                follow_redirects=True)
cs1 = db_q("SELECT * FROM consultations WHERE from_user=(SELECT id FROM users WHERE username='userD')")
check("咨询提交成功且待回答", "咨询已提交" in r_cs1.get_data(as_text=True) and len(cs1) == 1
      and cs1[0]["status"] == "pending")
check("咨询支付扣10积分", points_of("userD") == 40, "当前=%s" % points_of("userD"))
# 3. 不能向自己的资料咨询
r_cs2 = cA.post("/doc/%d/consult" % doc1["id"], data={"question": "自己咨询自己测试问题"},
                follow_redirects=True)
check("不能向自己资料咨询", "不能向自己的资料" in r_cs2.get_data(as_text=True))
# 4. 非作者回答被拒(403)
r_aw0 = cB.post("/consult/%d/answer" % cs1[0]["id"], data={"answer": "我不是作者随便答"})
check("非作者回答咨询被拒(403)", r_aw0.status_code == 403)
# 5. 作者回答成功并结算
pts_a0 = points_of("userA")
r_aw1 = cA.post("/consult/%d/answer" % cs1[0]["id"],
                data={"answer": "需要提前在医院公众号预约，周末病案室不上班。"}, follow_redirects=True)
cs1a = db_q("SELECT * FROM consultations WHERE id=?", (cs1[0]["id"],))[0]
check("作者回答成功", "回答成功" in r_aw1.get_data(as_text=True) and cs1a["status"] == "answered")
check("作者获得咨询酬劳(+10)", points_of("userA") == pts_a0 + 10, "变动=%s" % (points_of("userA") - pts_a0))
# 6. 已回答的咨询不能关闭退款
r_cl0 = cD.post("/consult/%d/close" % cs1[0]["id"], follow_redirects=True)
check("已回答咨询不能关闭退款", "仅未回答的咨询可关闭" in r_cl0.get_data(as_text=True)
      and points_of("userD") == 40)
# 7. 新咨询未回答可关闭退款
cD.post("/doc/%d/consult" % doc1["id"], data={"question": "再问一个准备关闭的问题"},
        follow_redirects=True)
cs2 = db_q("SELECT * FROM consultations WHERE from_user=(SELECT id FROM users WHERE username='userD')"
           " AND status='pending'")
r_cl1 = cD.post("/consult/%d/close" % cs2[0]["id"], follow_redirects=True)
check("未回答咨询可关闭退款", "已退回" in r_cl1.get_data(as_text=True)
      and points_of("userD") == 40, "当前=%s" % points_of("userD"))
# 8. 咨询API
r_ca = cD.get("/api/consultations")
check("咨询API返回提问与收到列表", r_ca.status_code == 200 and r_ca.get_json()["ok"]
      and len(r_ca.get_json()["asked"]) >= 2)
check("未登录咨询API返回401", client_factory().get("/api/consultations").status_code == 401)

# ---------------- 测试二十：资料需求悬赏 ----------------
print("\n【测试二十】需求悬赏：发布冻结→应征→采纳结算/关闭退款")
# 1. 积分不足发布被拒
r_bt0 = cB.post("/bounty/new", data={"title": "求一份不存在的超级贵资料", "reward_points": "99999"},
                follow_redirects=True)
check("积分不足发布悬赏被拒", "积分不足" in r_bt0.get_data(as_text=True))
# 2. 低于最低积分被拒
r_bt1 = cB.post("/bounty/new", data={"title": "求一份测试用低分资料", "reward_points": "5"},
                follow_redirects=True)
check("低于最低积分发布被拒", "至少" in r_bt1.get_data(as_text=True))
# 3. 正常发布：冻结50积分
pts_c2 = points_of("userC")
r_bt2 = cC.post("/bounty/new", data={"title": "求泸州市医院出院记录新版样式",
                                     "province": "四川省", "city": "泸州市",
                                     "doc_type": "出院记录", "reward_points": "50",
                                     "description": "需要2026年最新格式"}, follow_redirects=True)
bt1 = db_q("SELECT * FROM bounties WHERE title='求泸州市医院出院记录新版样式'")
check("悬赏发布成功且冻结积分", "发布成功" in r_bt2.get_data(as_text=True) and len(bt1) == 1
      and points_of("userC") == pts_c2 - 50)
# 4. 悬赏大厅公开可见
r_bh = client_factory().get("/bounty")
check("悬赏大厅公开展示进行中悬赏", "求泸州市医院出院记录新版样式" in r_bh.get_data(as_text=True))
# 5. 不能应征自己的悬赏
r_be0 = cC.post("/bounty/%d/enter" % bt1[0]["id"], data={"document_id": str(doc_vv["id"])},
                follow_redirects=True)
check("不能应征自己的悬赏", "不能应征自己" in r_be0.get_data(as_text=True))
# 6. 他人资料不能用于应征
r_be1 = cA.post("/bounty/%d/enter" % bt1[0]["id"], data={"document_id": str(doc_vv["id"])},
                follow_redirects=True)
check("不能用他人资料应征", "只能选择本人已发布的资料" in r_be1.get_data(as_text=True))
# 7. 正常应征
r_be2 = cA.post("/bounty/%d/enter" % bt1[0]["id"], data={"document_id": str(doc1["id"])},
                follow_redirects=True)
en1 = db_q("SELECT * FROM bounty_entries WHERE bounty_id=?", (bt1[0]["id"],))
check("应征成功生成待确认记录", "应征成功" in r_be2.get_data(as_text=True) and len(en1) == 1
      and en1[0]["status"] == "pending")
# 8. 重复应征同一资料被拒
r_be3 = cA.post("/bounty/%d/enter" % bt1[0]["id"], data={"document_id": str(doc1["id"])},
                follow_redirects=True)
check("重复应征同一资料被拒", "已用该资料应征过" in r_be3.get_data(as_text=True))
# 9. 非发布者采纳被拒(403)
r_ac0 = cB.post("/bounty/%d/accept/%d" % (bt1[0]["id"], en1[0]["id"]))
check("非发布者采纳被拒(403)", r_ac0.status_code == 403)
# 10. 发布者采纳：积分结算给应征者
pts_a1 = points_of("userA")
r_ac1 = cC.post("/bounty/%d/accept/%d" % (bt1[0]["id"], en1[0]["id"]), follow_redirects=True)
bt1a = db_q("SELECT * FROM bounties WHERE id=?", (bt1[0]["id"],))[0]
check("采纳成功悬赏达成", "已采纳" in r_ac1.get_data(as_text=True) and bt1a["status"] == "fulfilled")
check("应征者获得悬赏积分(+50)", points_of("userA") == pts_a1 + 50,
      "变动=%s" % (points_of("userA") - pts_a1))
# 11. 关闭悬赏退款
pts_c3 = points_of("userC")
cC.post("/bounty/new", data={"title": "求一份用于关闭测试的资料", "reward_points": "20"},
        follow_redirects=True)
bt2 = db_q("SELECT * FROM bounties WHERE title='求一份用于关闭测试的资料'")[0]
cB.post("/bounty/%d/enter" % bt2["id"], data={"document_id": str(doc_nv["id"])}, follow_redirects=True)
r_cc = cC.post("/bounty/%d/close" % bt2["id"], follow_redirects=True)
check("关闭悬赏退回冻结积分", "已退回" in r_cc.get_data(as_text=True)
      and points_of("userC") == pts_c3, "变动=%s" % (points_of("userC") - pts_c3))
check("关闭后悬赏状态为closed",
      db_q("SELECT status FROM bounties WHERE id=?", (bt2["id"],))[0]["status"] == "closed")
# 12. 悬赏API
r_ba = client_factory().get("/api/bounties")
check("悬赏API返回JSON列表", r_ba.status_code == 200 and r_ba.get_json()["ok"]
      and isinstance(r_ba.get_json()["items"], list))

# ---------------- 收尾 ----------------
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 62)
print("测试完成：通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
