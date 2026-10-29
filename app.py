# -*- coding: utf-8 -*-
"""
资料百事通 V3.0 —— 全民共创式资料共享与智能查询平台
================================================================
作者：军哥懂保（微信：xunijiayuan）
阶段：第五阶段 V3.0（商业化：VIP会员 + 广告激励 + 作者付费咨询 + 资料需求悬赏）
说明：Demo阶段以积分模拟支付结算，不接入真实支付通道；所有权益校验均在服务端完成。

技术架构（选择理由）：
  - 后端 Flask：轻量稳定、单机可运行、易维护，适合小团队持续迭代
  - 数据库 SQLite：零配置真实持久化，通过统一封装便于未来迁移 MySQL
  - 前端 Jinja2 + 原生 HTML/CSS/JS：响应式覆盖电脑+手机浏览器，无构建依赖
  - 图片 Pillow 重编码：清除 EXIF 元数据（含GPS等隐私）、压平图片
  - 预留 /api/* JSON 接口与 /api/ai/search 智能搜索接口（V1.5 接入）

未来模块预留（本阶段不实现）：资料版本管理、纠错反馈、AI检索、
社区讨论、贡献等级、会员付费 —— 数据库设计已考虑可扩展性。
"""
import io
import os
import uuid
import sqlite3
import zipfile
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, g, request, session, redirect, url_for, send_file,
                   render_template, jsonify, send_from_directory, abort, flash)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageOps
import ai_service
from seed_data import seed_full_categories

# ---------------------------------------------------------------- 基础配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ziliaobaishitong.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
SECRET_FILE = os.path.join(BASE_DIR, "secret_key.txt")

APP_VERSION = "V3.0"
APP_AUTHOR = "军哥懂保"
APP_WECHAT = "xunijiayuan"

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024          # 单张图片上限 10MB
MAX_IMAGES = 9                              # 单条资料最多图片数
PAGE_SIZE = 12                              # 搜索分页大小

Image.MAX_IMAGE_PIXELS = 30_000_000         # 防解压炸弹

os.makedirs(UPLOAD_DIR, exist_ok=True)


def _load_secret_key():
    """持久化密钥：重启服务后会话不失效"""
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = uuid.uuid4().hex + uuid.uuid4().hex
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _dir_stat():
    """统计程序当前大小与最后保存时间（界面展示用）"""
    total = 0
    last = 0
    for root, _dirs, files in os.walk(BASE_DIR):
        if ".git" in root:
            continue
        for fn in files:
            try:
                p = os.path.join(root, fn)
                total += os.path.getsize(p)
                last = max(last, os.path.getmtime(p))
            except OSError:
                pass
    return round(total / 1024 / 1024, 2), datetime.fromtimestamp(last) if last else datetime.now()


APP_SIZE_MB, APP_LAST_SAVE = _dir_stat()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
)

# ---------------------------------------------------------------- 数据库
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nickname TEXT,
    role TEXT DEFAULT 'user',            -- user/admin，预留角色扩展
    status INTEGER DEFAULT 1,
    region TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS industries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    icon TEXT DEFAULT '',
    is_open INTEGER DEFAULT 0,           -- 1 开放 / 0 暂未开放（后台可配置）
    sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    industry_id INTEGER NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    county TEXT DEFAULT '',
    org_name TEXT NOT NULL,               -- 用途机构（使用该资料的机构，如保险公司）
    source_org TEXT DEFAULT '',           -- 来源机构（出具该资料的机构，如医院）
    title TEXT NOT NULL,
    business TEXT DEFAULT '',
    doc_type TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    purpose TEXT DEFAULT '',
    obtain_method TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT '',
    valid_period TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',       -- pending待审核/published已发布/rejected已驳回/offline已下架
    reject_reason TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS document_images(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    ref_type TEXT DEFAULT 'doc',        -- doc=主资料 / version=资料版本（V3.0修复：避免id冲突导致图片交叉）
    filename TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,
    user_id INTEGER,
    admin_id INTEGER,
    action TEXT,
    detail TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_industry ON documents(industry_id);
CREATE INDEX IF NOT EXISTS idx_img_doc ON document_images(document_id);
"""

# V1.0 新增表：分类配置 + 版本管理 + 纠错反馈
# document_versions 资料版本 | feedback 纠错反馈 | discussion 讨论
# contribution_points 贡献积分 | membership 会员 | service_order 订单

CATEGORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER DEFAULT 0,           -- 0=顶级（行业），其他=父级ID
    type TEXT NOT NULL,                    -- industry/region/org/business/doc_type
    name TEXT NOT NULL,
    sort INTEGER DEFAULT 0,
    status INTEGER DEFAULT 1,              -- 1启用/0禁用
    created_at TEXT,
    UNIQUE(parent_id, type, name)
);
CREATE INDEX IF NOT EXISTS idx_cat_parent ON categories(parent_id, type);
"""

VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,          -- 主资料ID（逻辑组）
    version_no INTEGER DEFAULT 1,          -- 版本号
    user_id INTEGER NOT NULL,
    industry_id INTEGER NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    county TEXT DEFAULT '',
    org_name TEXT NOT NULL,
    source_org TEXT DEFAULT '',
    title TEXT NOT NULL,
    business TEXT DEFAULT '',
    doc_type TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    purpose TEXT DEFAULT '',
    obtain_method TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    source TEXT DEFAULT '',
    valid_period TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',         -- pending待审核/published已发布/rejected已驳回/history历史
    reject_reason TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    is_current INTEGER DEFAULT 0,          -- 1=当前默认展示版本
    created_at TEXT,
    updated_at TEXT,
    published_at TEXT,
    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dv_doc ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_dv_current ON document_versions(document_id, is_current);
CREATE INDEX IF NOT EXISTS idx_dv_status ON document_versions(status);
"""

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,                    -- helpful/error/outdated
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',         -- pending待处理/processed已处理/ignored已忽略
    admin_note TEXT DEFAULT '',
    created_at TEXT,
    processed_at TEXT,
    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fb_doc ON feedback(document_id);
CREATE INDEX IF NOT EXISTS idx_fb_status ON feedback(status);
"""

# V2.0 新增表：社区讨论 + 贡献积分
DISCUSSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS discussions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    parent_id INTEGER DEFAULT 0,         -- 0=顶级讨论，其他=回复的讨论ID
    content TEXT NOT NULL,
    status TEXT DEFAULT 'published',     -- published正常/hidden管理员隐藏
    created_at TEXT,
    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dis_doc ON discussions(document_id, status);
CREATE INDEX IF NOT EXISTS idx_dis_user ON discussions(user_id, created_at);
"""

POINTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS points_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL,             -- 积分变动（正为加，负为消费）
    action TEXT NOT NULL,                -- upload_approved/new_version/feedback_processed/discussion/vip_redeem/ad_reward/consult_*/bounty_*
    detail TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pl_user ON points_log(user_id);
"""

# V3.0 新增表：会员 / 广告激励 / 付费咨询 / 需求悬赏
V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS membership(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    level TEXT DEFAULT 'vip',            -- vip（预留更多等级）
    days INTEGER DEFAULT 30,
    source TEXT DEFAULT 'points',        -- points积分兑换 / admin管理员开通
    expire_at TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ms_user ON membership(user_id, expire_at);
CREATE TABLE IF NOT EXISTS ad_rewards(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reward_points INTEGER NOT NULL,
    ad_scene TEXT DEFAULT 'daily',       -- daily每日激励（预留更多场景）
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ar_user ON ad_rewards(user_id, created_at);
CREATE TABLE IF NOT EXISTS consultations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    from_user INTEGER NOT NULL,          -- 提问者
    to_user INTEGER NOT NULL,            -- 资料作者
    question TEXT NOT NULL,
    answer TEXT DEFAULT '',
    price INTEGER NOT NULL,              -- 支付积分（Demo模拟支付）
    status TEXT DEFAULT 'pending',       -- pending待回答/answered已回答/refunded已退款
    created_at TEXT,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cs_to ON consultations(to_user, status);
CREATE INDEX IF NOT EXISTS idx_cs_from ON consultations(from_user, status);
CREATE TABLE IF NOT EXISTS bounties(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    province TEXT DEFAULT '',
    city TEXT DEFAULT '',
    org_name TEXT DEFAULT '',
    doc_type TEXT DEFAULT '',
    reward_points INTEGER NOT NULL,
    status TEXT DEFAULT 'open',          -- open进行中/fulfilled已达成/closed已关闭
    created_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_status ON bounties(status, created_at);
CREATE TABLE IF NOT EXISTS bounty_entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bounty_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',       -- pending待确认/accepted已采纳/rejected未采纳
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_be_bounty ON bounty_entries(bounty_id, status);
"""

# V3.0 商业化配置（Demo阶段：积分模拟支付，不接入真实支付通道）
VIP_PLANS = [                            # (天数, 所需积分, 套餐名)
    (30, 200, "月度会员"),
    (365, 2000, "年度会员"),
]
CONSULT_PRICE = 10                       # 付费咨询统一价格（积分）
AD_REWARD_POINTS = 5                     # 每次广告激励积分
AD_DAILY_LIMIT = 3                       # 每日广告激励上限
BOUNTY_MIN_POINTS = 10                   # 悬赏最低积分

# 贡献等级体系：积分门槛 → (等级名, 每日发言额度)
CONTRIBUTION_LEVELS = [
    (0,   "Lv1 新手",      5),
    (10,  "Lv2 热心人",    10),
    (30,  "Lv3 贡献者",    20),
    (70,  "Lv4 资深贡献者", 50),
    (150, "Lv5 社区之星",  100),
]

# 积分规则
POINTS_RULES = {
    "upload_approved":    10,   # 上传资料审核通过
    "new_version":         5,   # 提交资料新版本
    "feedback_processed":  2,   # 纠错反馈被管理员采纳处理
    "discussion":          1,   # 发表讨论
}

INIT_INDUSTRIES = [
    ("保险理赔", "shield", 1, 1, "理赔资料 · 申请流程"),
    ("医疗健康", "medical", 0, 2, "就医资料 · 报销流程"),
    ("政务办事", "gov", 0, 3, "办事材料 · 办理流程"),
    ("教育升学", "edu", 0, 4, "升学资料 · 办理流程"),
    ("房产相关", "house", 0, 5, "房产资料 · 过户流程"),
    ("交通出行", "car", 0, 6, "车务资料 · 办理流程"),
    ("金融理财", "finance", 0, 7, "理财资料 · 办理流程"),
    ("法务", "legal", 1, 8, "法律文书 · 维权流程"),
]

PROVINCES = ["北京市", "天津市", "上海市", "重庆市", "河北省", "山西省", "辽宁省", "吉林省",
             "黑龙江省", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省",
             "湖北省", "湖南省", "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省",
             "甘肃省", "青海省", "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区",
             "新疆维吾尔自治区", "香港特别行政区", "澳门特别行政区", "其他/境外"]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    lastid = cur.lastrowid
    cur.close()
    return lastid


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(DB_SCHEMA)
    db.executescript(CATEGORIES_SCHEMA)
    db.executescript(VERSIONS_SCHEMA)
    db.executescript(FEEDBACK_SCHEMA)
    db.executescript(DISCUSSIONS_SCHEMA)
    db.executescript(POINTS_SCHEMA)
    db.executescript(V3_SCHEMA)
    # V3.0.1 迁移：documents/document_versions 增加 source_org 列
    for tbl in ("documents", "document_versions"):
        try:
            db.execute("ALTER TABLE %s ADD COLUMN source_org TEXT DEFAULT ''" % tbl)
        except sqlite3.OperationalError:
            pass  # 列已存在，忽略
    # V3.0 迁移：document_images 增加 ref_type 列（区分主资料/版本图片）
    try:
        db.execute("ALTER TABLE document_images ADD COLUMN ref_type TEXT DEFAULT 'doc'")
    except sqlite3.OperationalError:
        pass
    # 迁移：industries 增加 subtitle 列（首页行业卡片副标题，按行业分别配置）
    try:
        db.execute("ALTER TABLE industries ADD COLUMN subtitle TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    cur = db.execute("SELECT COUNT(*) FROM industries")
    if cur.fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO industries(name,icon,is_open,sort,subtitle) VALUES(?,?,?,?,?)",
            INIT_INDUSTRIES)
    cur = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if cur.fetchone()[0] == 0:
        db.execute(
            "INSERT INTO users(username,password_hash,nickname,role,created_at) VALUES(?,?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "平台管理员", "admin", now_str()))
    # 初始化默认分类（保险理赔行业示例）
    cur = db.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        now = now_str()
        # 顶级：行业
        db.execute("INSERT INTO categories(parent_id,type,name,sort,created_at) VALUES(0,'industry','保险理赔',1,?)", (now,))
        ind_id = db.execute("SELECT id FROM categories WHERE name='保险理赔' AND parent_id=0").fetchone()[0]
        # 机构
        for i, name in enumerate(["平安人寿", "中国人寿", "太平洋保险", "泰康人寿", "新华保险"]):
            db.execute("INSERT INTO categories(parent_id,type,name,sort,created_at) VALUES(?,?,?,?,?)",
                       (ind_id, "org", name, i+1, now))
        # 业务
        for i, name in enumerate(["住院医疗理赔", "意外医疗理赔", "重疾理赔", "身故理赔", "门诊理赔"]):
            db.execute("INSERT INTO categories(parent_id,type,name,sort,created_at) VALUES(?,?,?,?,?)",
                       (ind_id, "business", name, i+1, now))
        # 资料类型
        for i, name in enumerate(["出院记录", "出院证明", "诊断证明", "医疗费用发票", "住院费用明细清单",
                                   "医保结算单", "理赔申请书", "病历本", "检查报告", "手术记录"]):
            db.execute("INSERT INTO categories(parent_id,type,name,sort,created_at) VALUES(?,?,?,?,?)",
                       (ind_id, "doc_type", name, i+1, now))
    # 全行业分类默认值补齐（幂等，已有分类不重复）\n    seed_full_categories(db, now_str())\n    db.commit()
    db.close()


def add_log(action, document_id=None, user_id=None, admin_id=None, detail=""):
    execute(
        "INSERT INTO audit_logs(document_id,user_id,admin_id,action,detail,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (document_id, user_id, admin_id, action, detail, now_str()))


# ---------------------------------------------------------------- 认证工具
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = query("SELECT * FROM users WHERE id=? AND status=1", (uid,), one=True)
    return u


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "请先登录"}), 401
            flash("请先登录后再操作", "warn")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "请先登录"}), 401
            return redirect(url_for("login", next=request.path))
        if u["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "需要管理员权限"}), 403
            abort(403)
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- 图片处理
def save_image(file_storage):
    """校验并用 Pillow 重编码保存：清除EXIF元数据、压平图层，防止隐私泄露"""
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("不支持的图片格式：%s（仅支持 JPG/PNG/WebP）" % (ext or "未知"))
    data = file_storage.read()
    if len(data) == 0:
        raise ValueError("图片内容为空")
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError("图片超过10MB限制：%s" % filename)
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)          # 修正旋转方向
        w, h = img.size
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGBA")
            fmt = "PNG"
            ext = "png"
        else:
            img = img.convert("RGB")
            fmt = "JPEG"
            ext = "jpg"
        buf = io.BytesIO()
        img.save(buf, format=fmt, quality=88, optimize=True)  # 重编码=清除全部元数据
        buf.seek(0)
    except ValueError:
        raise
    except Exception:
        raise ValueError("图片文件损坏或不是有效图片：%s" % filename)
    name = uuid.uuid4().hex + "." + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(buf.read())
    return name, (w, h)


def doc_cover(doc_id):
    r = query("SELECT filename FROM document_images WHERE document_id=? ORDER BY sort_order,id LIMIT 1",
              (doc_id,), one=True)
    return r["filename"] if r else None


def doc_images(doc_id, ref_type='doc'):
    return query("SELECT * FROM document_images WHERE document_id=? AND ref_type=? ORDER BY sort_order,id",
                 (doc_id, ref_type))


# ---------------------------------------------------------------- 分类工具
def cat_children(parent_id, cat_type):
    """获取指定父级下的启用分类"""
    return query(
        "SELECT * FROM categories WHERE parent_id=? AND type=? AND status=1 ORDER BY sort",
        (parent_id, cat_type))


def industries_with_status():
    """行业列表 + 真实可用状态

    修复逻辑：industries 与 categories 是两张独立表，id 不可混用。
    统一以 industries.id 作为上传/筛选用的 industry_id，
    再按行业名称关联 categories 顶级节点，判断该行业是否已配置子分类。
    """
    inds = query("SELECT id,name,icon,is_open,sort,COALESCE(subtitle,'') AS subtitle FROM industries ORDER BY sort")
    tops = query("SELECT id,name FROM categories WHERE parent_id=0 AND type='industry' AND status=1")
    top_by_name = {t["name"]: t["id"] for t in tops}
    kid_counts = {r["parent_id"]: r["c"] for r in query(
        "SELECT parent_id,COUNT(*) c FROM categories"
        " WHERE type IN ('org','business','doc_type') AND status=1 GROUP BY parent_id")}
    items = []
    for i in inds:
        row = dict(i)
        cid = top_by_name.get(i["name"])
        row["cat_id"] = cid
        row["child_count"] = kid_counts.get(cid, 0)
        row["ready"] = bool(i["is_open"] and row["child_count"] > 0)
        items.append(row)
    return items


def usable_industries(include_id=None):
    """可选用行业：已开放且子分类已配置；编辑/改版时可保留该资料的历史行业"""
    items = [i for i in industries_with_status() if i["ready"]]
    if include_id and all(i["id"] != include_id for i in items):
        row = query("SELECT id,name,icon,is_open,sort,COALESCE(subtitle,'') AS subtitle FROM industries WHERE id=?", (include_id,), one=True)
        if row:
            r = dict(row)
            r["cat_id"] = None
            r["child_count"] = 0
            r["ready"] = False
            items.append(r)
    return items


def cat_tree():
    """获取完整分类树（供后台管理）"""
    rows = query("SELECT * FROM categories ORDER BY parent_id,sort")
    tree = {}
    for r in rows:
        pid = r["parent_id"]
        if pid not in tree:
            tree[pid] = []
        tree[pid].append(dict(r))
    return tree


# ---------------------------------------------------------------- 贡献等级与发言额度（V2.0）
def change_points(user_id, delta, action, detail=""):
    """积分变动并留痕（V3.0：支持负数=消费结算）"""
    execute("INSERT INTO points_log(user_id,points,action,detail,created_at) VALUES(?,?,?,?,?)",
            (user_id, delta, action, detail, now_str()))


def award_points(user_id, action, detail=""):
    """给用户加积分并留痕"""
    pts = POINTS_RULES.get(action, 0)
    if pts <= 0:
        return
    change_points(user_id, pts, action, detail)


def user_points(user_id):
    """用户总积分"""
    r = query("SELECT COALESCE(SUM(points),0) s FROM points_log WHERE user_id=?",
              (user_id,), one=True)
    return r["s"] if r else 0


def user_level(user_id):
    """用户等级信息：返回 dict(等级名/额度/总积分/下一级差距)"""
    pts = user_points(user_id)
    name, quota = CONTRIBUTION_LEVELS[0][1], CONTRIBUTION_LEVELS[0][2]
    next_need = 0
    for i, (threshold, lname, lquota) in enumerate(CONTRIBUTION_LEVELS):
        if pts >= threshold:
            name, quota = lname, lquota
            if i + 1 < len(CONTRIBUTION_LEVELS):
                next_need = CONTRIBUTION_LEVELS[i + 1][0] - pts
            else:
                next_need = 0
    return {"points": pts, "level_name": name, "daily_quota": quota, "next_need": next_need}


def today_discussion_count(user_id):
    """用户今日已发言次数"""
    today = datetime.now().strftime("%Y-%m-%d")
    return query("SELECT COUNT(*) c FROM discussions WHERE user_id=? AND created_at LIKE ?",
                 (user_id, today + "%"), one=True)["c"]


def discussion_quota_left(user_id, is_admin=False):
    """剩余发言额度；管理员不限"""
    if is_admin:
        return 9999
    lv = user_level(user_id)
    left = lv["daily_quota"] - today_discussion_count(user_id)
    return max(0, left)


# ---------------------------------------------------------------- 会员与广告（V3.0）
def user_vip(user_id):
    """用户当前有效会员记录（未过期），无则返回None"""
    return query("SELECT * FROM membership WHERE user_id=? AND expire_at>=?"
                 " ORDER BY expire_at DESC", (user_id, now_str()), one=True)


def today_ad_count(user_id):
    """用户今日已领取广告激励次数"""
    today = datetime.now().strftime("%Y-%m-%d")
    return query("SELECT COUNT(*) c FROM ad_rewards WHERE user_id=? AND created_at LIKE ?",
                 (user_id, today + "%"), one=True)["c"]


# ---------------------------------------------------------------- 公共上下文
@app.context_processor
def inject_globals():
    u = current_user()
    return {
        "APP_VERSION": APP_VERSION, "APP_AUTHOR": APP_AUTHOR, "APP_WECHAT": APP_WECHAT,
        "APP_SIZE_MB": APP_SIZE_MB, "APP_LAST_SAVE": APP_LAST_SAVE.strftime("%Y-%m-%d %H:%M"),
        "cur_user": u,
        "cur_vip": user_vip(u["id"]) if u else None,
        "provinces": PROVINCES,
    }


def fmt_row(d, with_images=False):
    """文档行 → 字典（供API使用）"""
    ind = query("SELECT name FROM industries WHERE id=?", (d["industry_id"],), one=True)
    u = query("SELECT nickname FROM users WHERE id=?", (d["user_id"],), one=True)
    item = {
        "id": d["id"], "title": d["title"],
        "industry": ind["name"] if ind else "",
        "province": d["province"], "city": d["city"], "county": d["county"],
        "org_name": d["org_name"], "source_org": d["source_org"] if "source_org" in d.keys() else "", "business": d["business"], "doc_type": d["doc_type"],
        "tags": [t for t in (d["tags"] or "").split("|") if t],
        "author": u["nickname"] if u else "匿名用户",
        "status": d["status"], "views": d["views"],
        "updated_at": d["updated_at"],
    }
    if with_images:
        item["images"] = [i["filename"] for i in doc_images(d["id"])]
        item.update({"purpose": d["purpose"], "obtain_method": d["obtain_method"],
                     "notes": d["notes"], "source": d["source"], "valid_period": d["valid_period"],
                     "reject_reason": d["reject_reason"]})
    return item


STATUS_MAP = {"pending": "待审核", "published": "已发布", "rejected": "已驳回", "offline": "已下架"}


# ---------------------------------------------------------------- 页面路由
@app.route("/")
def index():
    stats = {
        "docs": query("SELECT COUNT(*) c FROM documents WHERE status='published'", one=True)["c"],
        "users": query("SELECT COUNT(DISTINCT user_id) c FROM documents WHERE status='published'", one=True)["c"],
        "regions": query("SELECT COUNT(DISTINCT province) c FROM documents WHERE status='published'", one=True)["c"],
    }
    industries = industries_with_status()
    latest = query(
        """SELECT d.* FROM documents d WHERE d.status='published'
           ORDER BY d.published_at DESC LIMIT 8""")
    latest = [fmt_row(d) for d in latest]
    for it in latest:
        it["cover"] = doc_cover(it["id"])
    types = [r["doc_type"] for r in query(
        "SELECT DISTINCT doc_type FROM documents WHERE status='published' AND doc_type!='' LIMIT 8")]
    return render_template("index.html", stats=stats, industries=industries,
                           latest=latest, hot_types=types)


@app.route("/search")
def search():
    kw = (request.args.get("kw") or "").strip()
    industry = (request.args.get("industry") or "").strip()
    province = (request.args.get("province") or "").strip()
    city = (request.args.get("city") or "").strip()
    doc_type = (request.args.get("doc_type") or "").strip()
    sort = (request.args.get("sort") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    # V3.0：高级排序为VIP权益（服务端校验，普通用户忽略该参数）
    u0 = current_user()
    is_vip = bool(u0 and (user_vip(u0["id"]) or u0["role"] == "admin"))
    if not is_vip:
        sort = ""
    order_sql = "d.published_at DESC"
    if sort == "views":
        order_sql = "d.views DESC, d.published_at DESC"
    elif sort == "updated":
        order_sql = "d.updated_at DESC"

    where = ["d.status='published'"]
    args = []
    if kw:
        like = "%" + kw + "%"
        where.append("(d.title LIKE ? OR d.tags LIKE ? OR d.business LIKE ? OR d.org_name LIKE ?"
                     " OR d.source_org LIKE ? OR d.doc_type LIKE ? OR d.purpose LIKE ?)")
        args += [like] * 7
    if industry:
        where.append("d.industry_id IN (SELECT id FROM industries WHERE name LIKE ?)")
        args.append("%" + industry + "%")
    if province:
        where.append("d.province LIKE ?")
        args.append("%" + province + "%")
    if city:
        where.append("d.city LIKE ?")
        args.append("%" + city + "%")
    if doc_type:
        where.append("d.doc_type LIKE ?")
        args.append("%" + doc_type + "%")
    wsql = " AND ".join(where)

    total = query("SELECT COUNT(*) c FROM documents d WHERE " + wsql, tuple(args), one=True)["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    rows = query(
        """SELECT d.* FROM documents d WHERE %s
           ORDER BY %s LIMIT ? OFFSET ?""" % (wsql, order_sql),
        tuple(args) + (PAGE_SIZE, (page - 1) * PAGE_SIZE))
    items = []
    for d in rows:
        it = fmt_row(d)
        it["cover"] = doc_cover(it["id"])
        items.append(it)

    industries = industries_with_status()
    used_cities = [r["city"] for r in query(
        "SELECT DISTINCT city FROM documents WHERE status='published' ORDER BY city")]
    used_types = [r["doc_type"] for r in query(
        "SELECT DISTINCT doc_type FROM documents WHERE status='published' AND doc_type!='' ORDER BY doc_type")]
    return render_template("search.html", items=items, total=total, page=page, pages=pages,
                           industries=industries, used_cities=used_cities, used_types=used_types,
                           is_vip=is_vip,
                           f={"kw": kw, "industry": industry, "province": province,
                              "city": city, "doc_type": doc_type, "sort": sort})


@app.route("/doc/<int:doc_id>")
def detail(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    u = current_user()
    if not d:
        abort(404)
    # 权限：未发布资料仅作者本人和管理员可查看（后端强校验，非前端隐藏）
    if d["status"] != "published":
        if not u or (u["id"] != d["user_id"] and u["role"] != "admin"):
            abort(404)
    if d["status"] == "published" and request.args.get("nosee") != "1":
        execute("UPDATE documents SET views=views+1 WHERE id=?", (doc_id,))
    item = fmt_row(d, with_images=True)
    ind = query("SELECT * FROM industries WHERE id=?", (d["industry_id"],), one=True)
    author = query("SELECT id,nickname,region,created_at FROM users WHERE id=?", (d["user_id"],), one=True)
    author_docs = query(
        "SELECT COUNT(*) c FROM documents WHERE user_id=? AND status='published'", (d["user_id"],), one=True)["c"]
    images = doc_images(doc_id)
    img_names = [i["filename"] for i in images]
    # V2.0：加载讨论区（仅正常状态；隐藏的讨论仅管理员可见）
    dis_where = "ds.document_id=? AND ds.status='published'"
    if u and u["role"] == "admin":
        dis_where = "ds.document_id=?"
    dis_rows = query(
        """SELECT ds.*, us.nickname FROM discussions ds
           JOIN users us ON us.id=ds.user_id
           WHERE %s ORDER BY ds.created_at ASC LIMIT 200""" % dis_where, (doc_id,))
    discussions = []
    for r in dis_rows:
        item = dict(r)
        lv = user_level(r["user_id"])
        item["level_name"] = lv["level_name"]
        item["is_author"] = (r["user_id"] == d["user_id"])
        discussions.append(item)
    quota_left = discussion_quota_left(u["id"], u["role"] == "admin") if u else 0
    return render_template("detail.html", d=d, item=item, ind=ind, author=author,
                           author_docs=author_docs, images=images, img_names=img_names,
                           status_cn=STATUS_MAP.get(d["status"], d["status"]),
                           discussions=discussions, quota_left=quota_left)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------- 用户系统
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""
        nickname = (request.form.get("nickname") or "").strip()
        region = (request.form.get("region") or "").strip()
        if not username or len(username) < 3 or len(username) > 20:
            flash("用户名需3-20个字符", "warn")
        elif not nickname:
            flash("请填写昵称", "warn")
        elif len(password) < 6:
            flash("密码至少6位", "warn")
        elif password != password2:
            flash("两次输入的密码不一致", "warn")
        elif query("SELECT id FROM users WHERE username=?", (username,), one=True):
            flash("该用户名已被注册", "warn")
        else:
            uid = execute(
                "INSERT INTO users(username,password_hash,nickname,region,created_at) VALUES(?,?,?,?,?)",
                (username, generate_password_hash(password), nickname, region, now_str()))
            add_log("用户注册", user_id=uid, detail=username)
            session["uid"] = uid
            session.permanent = True
            flash("注册成功，欢迎加入资料百事通！", "ok")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        u = query("SELECT * FROM users WHERE username=? AND status=1", (username,), one=True)
        if u and check_password_hash(u["password_hash"], password):
            session["uid"] = u["id"]
            session.permanent = True
            add_log("用户登录", user_id=u["id"], detail=username)
            nxt = request.args.get("next") or request.form.get("next") or url_for("index")
            if not nxt.startswith("/"):
                nxt = url_for("index")
            flash("登录成功，欢迎回来！", "ok")
            return redirect(nxt)
        flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "ok")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- 上传与编辑
def validate_doc_form(form):
    """服务端表单校验（权限与数据完整性必须在后端验证）"""
    errors = []
    data = {k: (form.get(k) or "").strip() for k in
            ("province", "city", "county", "org_name", "source_org", "title", "business",
             "doc_type", "tags", "purpose", "obtain_method", "notes", "source", "valid_period")}
    # V1.0: 从隐藏字段获取实际文本值（下拉选择时select的value是分类ID，text字段是名称）
    for key in ("org_name", "business", "doc_type"):
        text_val = (form.get(key + "_text") or "").strip()
        if text_val:
            data[key] = text_val
    try:
        industry_id = int(form.get("industry_id") or 0)
    except ValueError:
        industry_id = 0
    ind = query("SELECT * FROM industries WHERE id=?", (industry_id,), one=True)
    if not ind:
        errors.append("请选择所属行业")
    elif not ind["is_open"]:
        errors.append("该行业暂未开放，请选择已开放行业")
    else:
        top = query("SELECT id FROM categories WHERE parent_id=0 AND type='industry' AND name=? AND status=1",
                    (ind["name"],), one=True)
        kids = query("""SELECT COUNT(*) c FROM categories
                        WHERE parent_id=? AND type IN ('org','business','doc_type') AND status=1""",
                     (top["id"],), one=True)["c"] if top else 0
        if kids == 0:
            errors.append("该行业的机构/业务/资料类型分类尚未配置完整，暂时无法上传，请选择其他行业")
    for key, label in (("province", "省份"), ("city", "城市"), ("org_name", "用途机构"),
                       ("source_org", "来源机构"), ("title", "资料名称"), ("business", "适用业务"),
                       ("tags", "关键词标签"), ("purpose", "资料用途"), ("obtain_method", "获取方式")):
        if not data[key]:
            errors.append("请填写【%s】" % label)
    if len(data["title"]) > 80:
        errors.append("资料名称过长（≤80字）")
    tags = "|".join([t.strip() for t in
                     data["tags"].replace("，", "|").replace(",", "|").replace("、", "|").split("|") if t.strip()])
    data["tags"] = tags[:200]
    return errors, data, industry_id


@app.route("/upload", methods=["GET", "POST"], endpoint="upload")
@login_required
def upload():
    industries = usable_industries()
    if request.method == "POST":
        errors, data, industry_id = validate_doc_form(request.form)
        files = [f for f in request.files.getlist("images") if f and f.filename]
        if not files:
            errors.append("请至少上传1张参考图片")
        if len(files) > MAX_IMAGES:
            errors.append("单条资料最多上传%d张图片" % MAX_IMAGES)
        confirmed = request.form.get("privacy_confirmed")
        if not confirmed:
            errors.append("请勾选隐私确认：确认已清除个人敏感信息或已完成脱敏处理")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("upload.html", industries=industries,
                                   form=request.form, edit_id=None), 400
        try:
            saved = []
            for f in files:
                name, _size = save_image(f)
                saved.append(name)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("upload.html", industries=industries,
                                   form=request.form, edit_id=None), 400
        t = now_str()
        doc_id = execute(
            """INSERT INTO documents
               (user_id,industry_id,province,city,county,org_name,source_org,title,
                business,doc_type,tags,purpose,obtain_method,notes,source,valid_period,
                status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (session["uid"], industry_id, data["province"], data["city"], data["county"],
             data["org_name"], data["source_org"], data["title"], data["business"], data["doc_type"], data["tags"],
             data["purpose"], data["obtain_method"], data["notes"], data["source"],
             data["valid_period"], t, t))
        for i, name in enumerate(saved):
            execute("INSERT INTO document_images(document_id,ref_type,filename,sort_order,created_at) VALUES(?,?,?,?,?)",
                    (doc_id, 'doc', name, i, t))
        add_log("上传资料", document_id=doc_id, user_id=session["uid"], detail=data["title"])
        flash("资料提交成功，已进入待审核队列，审核通过后将公开展示！", "ok")
        return redirect(url_for("my"))
    return render_template("upload.html", industries=industries, form={}, edit_id=None)


@app.route("/my")
@login_required
def my():
    rows = query(
        """SELECT d.*, i.name AS industry_name FROM documents d
           JOIN industries i ON i.id=d.industry_id
           WHERE d.user_id=? ORDER BY d.updated_at DESC""", (session["uid"],))
    items = []
    for d in rows:
        it = fmt_row(d)
        it["cover"] = doc_cover(it["id"])
        it["status_cn"] = STATUS_MAP.get(d["status"], d["status"])
        it["img_count"] = query("SELECT COUNT(*) c FROM document_images WHERE document_id=?",
                                (d["id"],), one=True)["c"]
        items.append(it)
    n_pub = sum(1 for x in items if x["status"] == "published")
    n_pen = sum(1 for x in items if x["status"] == "pending")
    # V3.0：积分 / 会员 / 付费咨询数据
    uid = session["uid"]
    consults_asked = query(
        """SELECT cs.*, d.title AS doc_title, uu.nickname AS to_name FROM consultations cs
           JOIN documents d ON d.id=cs.document_id JOIN users uu ON uu.id=cs.to_user
           WHERE cs.from_user=? ORDER BY cs.created_at DESC LIMIT 50""", (uid,))
    consults_recv = query(
        """SELECT cs.*, d.title AS doc_title, uu.nickname AS from_name FROM consultations cs
           JOIN documents d ON d.id=cs.document_id JOIN users uu ON uu.id=cs.from_user
           WHERE cs.to_user=? ORDER BY cs.created_at DESC LIMIT 50""", (uid,))
    return render_template("my.html", items=items, n_pub=n_pub, n_pen=n_pen,
                           points=user_points(uid), level=user_level(uid),
                           vip=user_vip(uid),
                           consults_asked=consults_asked, consults_recv=consults_recv)


@app.route("/my/edit/<int:doc_id>", methods=["GET", "POST"])
@login_required
def my_edit(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    # 权限强校验：只能修改自己的、且处于待审核/已驳回状态的资料
    if d["user_id"] != session["uid"]:
        abort(403)
    if d["status"] not in ("pending", "rejected"):
        flash("该资料当前状态（%s）不允许修改" % STATUS_MAP.get(d["status"], d["status"]), "warn")
        return redirect(url_for("my"))
    industries = usable_industries(include_id=d["industry_id"])
    if request.method == "POST":
        errors, data, industry_id = validate_doc_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("upload.html", industries=industries,
                                   form=request.form, edit_id=doc_id,
                                   old_images=doc_images(doc_id)), 400
        execute(
            """UPDATE documents SET industry_id=?,province=?,city=?,county=?,org_name=?,source_org=?,title=?,
               business=?,doc_type=?,tags=?,purpose=?,obtain_method=?,notes=?,source=?,valid_period=?,
               status='pending',reject_reason='',updated_at=? WHERE id=?""",
            (industry_id, data["province"], data["city"], data["county"], data["org_name"], data["source_org"],
             data["title"], data["business"], data["doc_type"], data["tags"], data["purpose"],
             data["obtain_method"], data["notes"], data["source"], data["valid_period"],
             now_str(), doc_id))
        # 处理图片：删除被勾选移除的旧图，追加新图
        remove_ids = request.form.getlist("remove_img")
        for rid in remove_ids:
            img = query("SELECT * FROM document_images WHERE id=? AND document_id=?", (rid, doc_id), one=True)
            if img:
                try:
                    os.remove(os.path.join(UPLOAD_DIR, img["filename"]))
                except OSError:
                    pass
                execute("DELETE FROM document_images WHERE id=?", (rid,))
        new_files = [f for f in request.files.getlist("images") if f and f.filename]
        exist_cnt = query("SELECT COUNT(*) c FROM document_images WHERE document_id=?",
                          (doc_id,), one=True)["c"]
        if exist_cnt + len(new_files) > MAX_IMAGES:
            flash("图片总数超过%d张，未追加新图片" % MAX_IMAGES, "warn")
        else:
            for i, f in enumerate(new_files):
                try:
                    name, _s = save_image(f)
                    execute("INSERT INTO document_images(document_id,filename,sort_order,created_at)"
                            " VALUES(?,?,?,?)", (doc_id, name, exist_cnt + i, now_str()))
                except ValueError as e:
                    flash(str(e), "error")
                    break
        add_log("修改资料", document_id=doc_id, user_id=session["uid"], detail=data["title"])
        flash("资料已更新并重新提交审核", "ok")
        return redirect(url_for("my"))
    images = doc_images(doc_id)
    return render_template("upload.html", industries=industries, form=dict(d),
                           edit_id=doc_id, old_images=images)


@app.route("/my/delete/<int:doc_id>", methods=["POST"])
@login_required
def my_delete(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    if d["user_id"] != session["uid"]:
        abort(403)
    if d["status"] not in ("pending", "rejected"):
        flash("已发布/已下架资料不可自行删除，如需处理请联系管理员", "warn")
        return redirect(url_for("my"))
    for img in doc_images(doc_id):
        try:
            os.remove(os.path.join(UPLOAD_DIR, img["filename"]))
        except OSError:
            pass
    execute("DELETE FROM document_images WHERE document_id=?", (doc_id,))
    execute("DELETE FROM documents WHERE id=?", (doc_id,))
    add_log("删除资料", document_id=doc_id, user_id=session["uid"], detail=d["title"])
    flash("资料已删除", "ok")
    return redirect(url_for("my"))


# ---------------------------------------------------------------- 管理后台
@app.route("/admin")
@admin_required
def admin():
    # V3.0：VIP用户资料优先审核（置顶显示）
    pending = query(
        """SELECT d.*, i.name AS industry_name, u.nickname,
           EXISTS(SELECT 1 FROM membership m WHERE m.user_id=d.user_id AND m.expire_at>=?) AS is_vip
           FROM documents d
           JOIN industries i ON i.id=d.industry_id JOIN users u ON u.id=d.user_id
           WHERE d.status='pending' ORDER BY is_vip DESC, d.created_at ASC""", (now_str(),))
    published = query(
        """SELECT d.*, i.name AS industry_name, u.nickname FROM documents d
           JOIN industries i ON i.id=d.industry_id JOIN users u ON u.id=d.user_id
           WHERE d.status IN ('published','offline','rejected')
           ORDER BY d.updated_at DESC LIMIT 50""")
    logs = query(
        """SELECT a.*, u.username AS admin_name FROM audit_logs a
           LEFT JOIN users u ON u.id=a.admin_id
           WHERE a.admin_id IS NOT NULL ORDER BY a.created_at DESC LIMIT 20""")
    pending = [dict(r) for r in pending]
    published = [dict(r) for r in published]
    for d in pending:
        d["cover"] = doc_cover(d["id"])
        d["img_count"] = query("SELECT COUNT(*) c FROM document_images WHERE document_id=?",
                               (d["id"],), one=True)["c"]
    for d in published:
        d["cover"] = doc_cover(d["id"])
        d["status_cn"] = STATUS_MAP.get(d["status"], d["status"])
    return render_template("admin.html", pending=pending, published=published, logs=logs)


@app.route("/admin/review/<int:doc_id>", methods=["POST"])
@admin_required
def admin_review(doc_id):
    action = request.form.get("action")
    reason = (request.form.get("reason") or "").strip()
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    if action == "approve":
        execute("UPDATE documents SET status='published',published_at=?,updated_at=?,reject_reason='' WHERE id=?",
                (now_str(), now_str(), doc_id))
        add_log("审核通过", document_id=doc_id, admin_id=session["uid"], detail=d["title"])
        award_points(d["user_id"], "upload_approved", "资料《%s》审核通过" % d["title"])
        flash("资料已审核通过并公开", "ok")
    elif action == "reject":
        if not reason:
            flash("驳回时必须填写驳回原因", "error")
            return redirect(url_for("admin"))
        execute("UPDATE documents SET status='rejected',reject_reason=?,updated_at=? WHERE id=?",
                (reason, now_str(), doc_id))
        add_log("审核驳回", document_id=doc_id, admin_id=session["uid"], detail=reason)
        flash("资料已驳回", "ok")
    else:
        flash("未知操作", "error")
    return redirect(url_for("admin"))


@app.route("/admin/offline/<int:doc_id>", methods=["POST"])
@admin_required
def admin_offline(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    execute("UPDATE documents SET status='offline',updated_at=? WHERE id=?", (now_str(), doc_id))
    add_log("下架资料", document_id=doc_id, admin_id=session["uid"], detail=d["title"])
    flash("资料已下架，不再公开展示", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/republish/<int:doc_id>", methods=["POST"])
@admin_required
def admin_republish(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    execute("UPDATE documents SET status='published',updated_at=? WHERE id=?", (now_str(), doc_id))
    add_log("重新上架", document_id=doc_id, admin_id=session["uid"], detail=d["title"])
    flash("资料已重新上架", "ok")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------- 分类管理（V1.0）
@app.route("/admin/categories")
@admin_required
def admin_categories():
    tree = cat_tree()
    industries = query("SELECT * FROM categories WHERE parent_id=0 AND type='industry' ORDER BY sort")
    return render_template("admin_categories.html", tree=tree, industries=industries)


@app.route("/admin/categories/add", methods=["POST"])
@admin_required
def admin_category_add():
    parent_id = request.form.get("parent_id", type=int) or 0
    cat_type = request.form.get("type") or ""
    name = (request.form.get("name") or "").strip()
    sort = request.form.get("sort", type=int) or 0
    if not name:
        flash("分类名称不能为空", "error")
        return redirect(url_for("admin_categories"))
    if cat_type not in ("industry", "region", "org", "business", "doc_type"):
        flash("分类类型无效", "error")
        return redirect(url_for("admin_categories"))
    # 检查重复
    exist = query("SELECT id FROM categories WHERE parent_id=? AND type=? AND name=?",
                  (parent_id, cat_type, name), one=True)
    if exist:
        flash("该分类已存在", "warn")
        return redirect(url_for("admin_categories"))
    execute("INSERT INTO categories(parent_id,type,name,sort,created_at) VALUES(?,?,?,?,?)",
            (parent_id, cat_type, name, sort, now_str()))
    add_log("添加分类", admin_id=session["uid"], detail="%s/%s" % (cat_type, name))
    flash("分类添加成功", "ok")
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/edit/<int:cat_id>", methods=["POST"])
@admin_required
def admin_category_edit(cat_id):
    c = query("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
    if not c:
        abort(404)
    name = (request.form.get("name") or "").strip()
    sort = request.form.get("sort", type=int) or 0
    status = 1 if request.form.get("status") == "on" else 0
    if not name:
        flash("分类名称不能为空", "error")
        return redirect(url_for("admin_categories"))
    execute("UPDATE categories SET name=?,sort=?,status=? WHERE id=?",
            (name, sort, status, cat_id))
    add_log("编辑分类", admin_id=session["uid"], detail="%s(%s)" % (name, c["type"]))
    flash("分类已更新", "ok")
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/del/<int:cat_id>", methods=["POST"])
@admin_required
def admin_category_del(cat_id):
    c = query("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
    if not c:
        abort(404)
    # 检查是否有子分类
    children = query("SELECT COUNT(*) c FROM categories WHERE parent_id=?", (cat_id,), one=True)["c"]
    if children > 0:
        flash("请先删除该分类下的子分类", "error")
        return redirect(url_for("admin_categories"))
    execute("DELETE FROM categories WHERE id=?", (cat_id,))
    add_log("删除分类", admin_id=session["uid"], detail="%s(%s)" % (c["name"], c["type"]))
    flash("分类已删除", "ok")
    return redirect(url_for("admin_categories"))


# ---------------------------------------------------------------- 分类级联API（供前端选择）
@app.route("/api/categories/tree")
def api_categories_tree():
    """获取完整分类树（JSON格式，供前端级联选择）

    修复：industry_id 统一使用 industries.id，再按名称映射到 categories 顶级节点，
    避免两张表的自增 id 混用导致除"保险理赔"外其余行业的选项全部为空。
    """
    result = []
    for ind in industries_with_status():
        cid = ind["cat_id"]
        result.append({
            "id": ind["id"],
            "name": ind["name"],
            "ready": ind["ready"],
            "orgs": [{"id": o["id"], "name": o["name"]} for o in cat_children(cid, "org")] if cid else [],
            "businesses": [{"id": b["id"], "name": b["name"]} for b in cat_children(cid, "business")] if cid else [],
            "doc_types": [{"id": d["id"], "name": d["name"]} for d in cat_children(cid, "doc_type")] if cid else [],
        })
    return jsonify({"ok": True, "items": result})


# ---------------------------------------------------------------- 版本管理（V1.0）
@app.route("/doc/<int:doc_id>/new_version", methods=["GET", "POST"])
@login_required
def new_version(doc_id):
    """提交资料新版本"""
    old_doc = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not old_doc:
        abort(404)
    if old_doc["status"] != "published":
        flash("只有已发布的资料才能提交新版本", "warn")
        return redirect(url_for("detail", doc_id=doc_id))
    industries = usable_industries(include_id=old_doc["industry_id"])
    if request.method == "POST":
        errors, data, industry_id = validate_doc_form(request.form)
        files = [f for f in request.files.getlist("images") if f and f.filename]
        if not files:
            errors.append("请至少上传1张参考图片")
        if len(files) > MAX_IMAGES:
            errors.append("单条资料最多上传%d张图片" % MAX_IMAGES)
        confirmed = request.form.get("privacy_confirmed")
        if not confirmed:
            errors.append("请勾选隐私确认")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("upload.html", industries=industries,
                                   form=request.form, edit_id=None, new_version_of=doc_id), 400
        try:
            saved = []
            for f in files:
                name, _size = save_image(f)
                saved.append(name)
        except ValueError as e:
            flash(str(e), "error")
            return render_template("upload.html", industries=industries,
                                   form=request.form, edit_id=None, new_version_of=doc_id), 400
        t = now_str()
        # 获取当前最大版本号（原始资料为v1，所以新版本从v2开始）
        max_ver_row = query("SELECT MAX(version_no) m FROM document_versions WHERE document_id=?",
                        (doc_id,), one=True)
        max_ver = max_ver_row["m"] if max_ver_row and max_ver_row["m"] is not None else 1
        new_ver = max_ver + 1
        vid = execute(
            """INSERT INTO document_versions(document_id,version_no,user_id,industry_id,province,city,county,
               org_name,source_org,title,business,doc_type,tags,purpose,obtain_method,notes,source,valid_period,
               status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (doc_id, new_ver, session["uid"], industry_id, data["province"], data["city"], data["county"],
             data["org_name"], data["source_org"], data["title"], data["business"], data["doc_type"], data["tags"],
             data["purpose"], data["obtain_method"], data["notes"], data["source"],
             data["valid_period"], t, t))
        # 保存版本图片
        for i, name in enumerate(saved):
            execute("INSERT INTO document_images(document_id,ref_type,filename,sort_order,created_at) VALUES(?,?,?,?,?)",
                    (vid, 'version', name, i, t))
        add_log("提交新版本", document_id=doc_id, user_id=session["uid"],
                detail="v%d %s" % (new_ver, data["title"]))
        award_points(session["uid"], "new_version", "《%s》v%d" % (data["title"], new_ver))
        flash("新版本已提交审核，审核通过后将替换旧版本", "ok")
        return redirect(url_for("detail", doc_id=doc_id))
    # GET：预填旧资料信息
    form_data = dict(old_doc)
    return render_template("upload.html", industries=industries, form=form_data,
                           edit_id=None, new_version_of=doc_id)


@app.route("/doc/<int:doc_id>/versions")
def doc_versions(doc_id):
    """查看资料历史版本"""
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    # 权限检查
    u = current_user()
    if d["status"] != "published":
        if not u or (u["id"] != d["user_id"] and u["role"] != "admin"):
            abort(404)
    versions = query(
        """SELECT dv.*, u.nickname FROM document_versions dv
           JOIN users u ON u.id=dv.user_id
           WHERE dv.document_id=? ORDER BY dv.version_no DESC""", (doc_id,))
    current = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    return render_template("versions.html", doc=d, versions=versions, current=current,
                           status_map=STATUS_MAP)


@app.route("/version/<int:vid>")
def view_version(vid):
    """查看单个历史版本详情"""
    v = query("SELECT * FROM document_versions WHERE id=?", (vid,), one=True)
    if not v:
        abort(404)
    d = query("SELECT * FROM documents WHERE id=?", (v["document_id"],), one=True)
    if not d:
        abort(404)
    # 权限：历史版本仅作者和管理员可查看，当前版本公开
    u = current_user()
    if v["status"] != "published" and v["is_current"] != 1:
        if not u or (u["id"] != v["user_id"] and u["role"] != "admin"):
            abort(404)
    images = doc_images(vid, ref_type='version')
    img_names = [i["filename"] for i in images]
    ind = query("SELECT * FROM industries WHERE id=?", (v["industry_id"],), one=True)
    author = query("SELECT id,nickname,region,created_at FROM users WHERE id=?", (v["user_id"],), one=True)
    return render_template("version_detail.html", v=v, doc=d, images=images, img_names=img_names,
                           ind=ind, author=author, status_cn=STATUS_MAP.get(v["status"], v["status"]))


# ---------------------------------------------------------------- 纠错反馈（V1.0）
@app.route("/doc/<int:doc_id>/feedback", methods=["POST"])
@login_required
def submit_feedback(doc_id):
    """提交资料反馈：有帮助/内容有误/已过时"""
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    fb_type = request.form.get("type") or ""
    reason = (request.form.get("reason") or "").strip()
    if fb_type not in ("helpful", "error", "outdated"):
        flash("反馈类型无效", "error")
        return redirect(url_for("detail", doc_id=doc_id))
    if fb_type in ("error", "outdated") and not reason:
        flash("请填写反馈理由", "error")
        return redirect(url_for("detail", doc_id=doc_id))
    # 检查是否已提交过同类型反馈
    exist = query("SELECT id FROM feedback WHERE document_id=? AND user_id=? AND type=?",
                  (doc_id, session["uid"], fb_type), one=True)
    if exist:
        flash("您已提交过该类型的反馈", "warn")
        return redirect(url_for("detail", doc_id=doc_id))
    execute("INSERT INTO feedback(document_id,user_id,type,reason,created_at) VALUES(?,?,?,?,?)",
            (doc_id, session["uid"], fb_type, reason, now_str()))
    add_log("提交反馈", document_id=doc_id, user_id=session["uid"],
            detail={"helpful": "有帮助", "error": "内容有误", "outdated": "已过时"}[fb_type])
    flash("感谢您的反馈，管理员会尽快核实", "ok")
    return redirect(url_for("detail", doc_id=doc_id))


# ---------------------------------------------------------------- 社区讨论（V2.0）
@app.route("/doc/<int:doc_id>/discuss", methods=["POST"])
@login_required
def post_discussion(doc_id):
    """发表讨论/回复（发言额度限制 + 积分奖励）"""
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d or d["status"] != "published":
        abort(404)
    content = (request.form.get("content") or "").strip()
    parent_id = request.form.get("parent_id", type=int) or 0
    if len(content) < 5:
        flash("讨论内容至少5个字", "error")
        return redirect(url_for("detail", doc_id=doc_id) + "#discussion")
    if len(content) > 500:
        flash("讨论内容过长（≤500字）", "error")
        return redirect(url_for("detail", doc_id=doc_id) + "#discussion")
    # 回复目标校验
    if parent_id:
        parent = query("SELECT * FROM discussions WHERE id=? AND document_id=?",
                       (parent_id, doc_id), one=True)
        if not parent or parent["status"] != "published":
            flash("回复的讨论不存在", "error")
            return redirect(url_for("detail", doc_id=doc_id) + "#discussion")
    # 发言额度校验（服务端强制，防灌水）
    u = current_user()
    left = discussion_quota_left(u["id"], u["role"] == "admin")
    if left <= 0:
        lv = user_level(u["id"])
        flash("今日发言额度已用完（%s 每日%d条），提升贡献等级可获得更多额度"
              % (lv["level_name"], lv["daily_quota"]), "warn")
        return redirect(url_for("detail", doc_id=doc_id) + "#discussion")
    execute("INSERT INTO discussions(document_id,user_id,parent_id,content,created_at)"
            " VALUES(?,?,?,?,?)",
            (doc_id, u["id"], parent_id, content, now_str()))
    add_log("发表讨论", document_id=doc_id, user_id=u["id"], detail=content[:30])
    award_points(u["id"], "discussion", "参与《%s》讨论" % d["title"][:20])
    flash("发表成功，感谢分享经验！", "ok")
    return redirect(url_for("detail", doc_id=doc_id) + "#discussion")


@app.route("/discuss/<int:dis_id>/delete", methods=["POST"])
@login_required
def delete_discussion(dis_id):
    """删除讨论：本人或管理员"""
    ds = query("SELECT * FROM discussions WHERE id=?", (dis_id,), one=True)
    if not ds:
        abort(404)
    u = current_user()
    if u["id"] != ds["user_id"] and u["role"] != "admin":
        abort(403)
    execute("DELETE FROM discussions WHERE id=? OR parent_id=?", (dis_id, dis_id))
    add_log("删除讨论", document_id=ds["document_id"],
            user_id=u["id"] if u["id"] == ds["user_id"] else None,
            admin_id=u["id"] if u["role"] == "admin" else None,
            detail="讨论#%d" % dis_id)
    flash("讨论已删除", "ok")
    return redirect(url_for("detail", doc_id=ds["document_id"]) + "#discussion")


@app.route("/admin/discuss/<int:dis_id>/hide", methods=["POST"])
@admin_required
def admin_hide_discussion(dis_id):
    """管理员隐藏/恢复讨论（违规内容处理）"""
    ds = query("SELECT * FROM discussions WHERE id=?", (dis_id,), one=True)
    if not ds:
        abort(404)
    new_status = "hidden" if ds["status"] == "published" else "published"
    execute("UPDATE discussions SET status=? WHERE id=?", (new_status, dis_id))
    add_log("隐藏讨论" if new_status == "hidden" else "恢复讨论",
            document_id=ds["document_id"], admin_id=session["uid"], detail="讨论#%d" % dis_id)
    flash("讨论已%s" % ("隐藏" if new_status == "hidden" else "恢复"), "ok")
    return redirect(url_for("detail", doc_id=ds["document_id"]) + "#discussion")


# ---------------------------------------------------------------- 社区高频问题AI整理（V2.0）
@app.route("/api/ai/community_digest")
def api_community_digest():
    """AI整理社区近期高频讨论问题（降级：关键词频率统计）"""
    rows = query(
        """SELECT ds.content, d.title AS doc_title FROM discussions ds
           JOIN documents d ON d.id=ds.document_id
           WHERE ds.status='published'
           ORDER BY ds.created_at DESC LIMIT 50""")
    items = [dict(r) for r in rows]
    result = ai_service.summarize_community(items)
    return jsonify(result)


# ---------------------------------------------------------------- 作者主页（V1.0）
@app.route("/author/<int:user_id>")
def author_profile(user_id):
    """作者公开主页"""
    u = query("SELECT id,nickname,region,created_at FROM users WHERE id=? AND status=1", (user_id,), one=True)
    if not u:
        abort(404)
    docs = query(
        """SELECT d.*, i.name AS industry_name FROM documents d
           JOIN industries i ON i.id=d.industry_id
           WHERE d.user_id=? AND d.status='published'
           ORDER BY d.published_at DESC""", (user_id,))
    items = []
    for d in docs:
        it = fmt_row(d)
        it["cover"] = doc_cover(it["id"])
        it["status_cn"] = STATUS_MAP.get(d["status"], d["status"])
        items.append(it)
    # 统计
    total_docs = len(items)
    total_views = sum(d["views"] for d in docs)
    helpful_count = query(
        "SELECT COUNT(*) c FROM feedback WHERE document_id IN (SELECT id FROM documents WHERE user_id=?) AND type='helpful'",
        (user_id,), one=True)["c"]
    discuss_count = query("SELECT COUNT(*) c FROM discussions WHERE user_id=? AND status='published'",
                          (user_id,), one=True)["c"]
    level = user_level(user_id)
    return render_template("author.html", author=u, items=items,
                           total_docs=total_docs, total_views=total_views,
                           helpful_count=helpful_count, discuss_count=discuss_count,
                           level=level)


# ---------------------------------------------------------------- 后台管理升级（V1.0）
@app.route("/admin/feedback")
@admin_required
def admin_feedback():
    """纠错反馈管理"""
    pending = query(
        """SELECT f.*, d.title AS doc_title, u.nickname AS user_name
           FROM feedback f
           JOIN documents d ON d.id=f.document_id
           JOIN users u ON u.id=f.user_id
           WHERE f.status='pending'
           ORDER BY f.created_at ASC""")
    processed = query(
        """SELECT f.*, d.title AS doc_title, u.nickname AS user_name
           FROM feedback f
           JOIN documents d ON d.id=f.document_id
           JOIN users u ON u.id=f.user_id
           WHERE f.status!='pending'
           ORDER BY f.processed_at DESC LIMIT 50""")
    return render_template("admin_feedback.html", pending=pending, processed=processed)


@app.route("/admin/feedback/<int:fb_id>", methods=["POST"])
@admin_required
def admin_feedback_process(fb_id):
    """处理反馈"""
    f = query("SELECT * FROM feedback WHERE id=?", (fb_id,), one=True)
    if not f:
        abort(404)
    action = request.form.get("action")
    note = (request.form.get("admin_note") or "").strip()
    if action == "process":
        execute("UPDATE feedback SET status='processed', admin_note=?, processed_at=? WHERE id=?",
                (note, now_str(), fb_id))
        add_log("处理反馈", document_id=f["document_id"], admin_id=session["uid"],
                detail="反馈#%d %s" % (fb_id, note[:30]))
        award_points(f["user_id"], "feedback_processed", "反馈#%d被采纳" % fb_id)
        flash("反馈已标记为已处理", "ok")
    elif action == "ignore":
        execute("UPDATE feedback SET status='ignored', admin_note=?, processed_at=? WHERE id=?",
                (note, now_str(), fb_id))
        add_log("忽略反馈", document_id=f["document_id"], admin_id=session["uid"],
                detail="反馈#%d" % fb_id)
        flash("反馈已忽略", "ok")
    return redirect(url_for("admin_feedback"))


@app.route("/admin/users")
@admin_required
def admin_users():
    """用户管理"""
    users = query("SELECT * FROM users ORDER BY created_at DESC")
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@admin_required
def admin_user_edit(user_id):
    """编辑用户"""
    u = query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    if not u:
        abort(404)
    action = request.form.get("action")
    if action == "toggle_status":
        new_status = 0 if u["status"] == 1 else 1
        execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
        add_log("禁用用户" if new_status == 0 else "启用用户",
                user_id=user_id, admin_id=session["uid"], detail=u["username"])
        flash("用户状态已更新", "ok")
    elif action == "toggle_role":
        if u["username"] == "admin":
            flash("不能修改内置管理员角色", "error")
        else:
            new_role = "admin" if u["role"] == "user" else "user"
            execute("UPDATE users SET role=? WHERE id=?", (new_role, user_id))
            add_log("调整角色", user_id=user_id, admin_id=session["uid"],
                    detail="%s → %s" % (u["username"], new_role))
            flash("用户角色已更新", "ok")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- V3.0 VIP会员中心
@app.route("/vip")
@login_required
def vip_center():
    """会员中心：状态/权益/积分兑换开通/广告激励入口"""
    u = current_user()
    return render_template(
        "vip.html", vip=user_vip(u["id"]), points=user_points(u["id"]),
        level=user_level(u["id"]), plans=VIP_PLANS,
        today_ads=today_ad_count(u["id"]), ad_limit=AD_DAILY_LIMIT,
        ad_reward=AD_REWARD_POINTS,
        history=query("SELECT * FROM membership WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
                      (u["id"],)))


@app.route("/vip/redeem", methods=["POST"])
@login_required
def vip_redeem():
    """积分兑换开通会员（Demo模拟支付：积分结算，未过期会员顺延天数）"""
    u = current_user()
    try:
        days = int(request.form.get("days") or 0)
    except ValueError:
        days = 0
    plan = None
    for p in VIP_PLANS:
        if p[0] == days:
            plan = p
            break
    if not plan:
        flash("无效的会员套餐", "error")
        return redirect(url_for("vip_center"))
    days, cost, name = plan
    pts = user_points(u["id"])
    if pts < cost:
        flash("积分不足：%s需%d积分，当前仅%d积分。可通过上传资料、看广告获得积分"
              % (name, cost, pts), "error")
        return redirect(url_for("vip_center"))
    cur = user_vip(u["id"])
    base = datetime.strptime(cur["expire_at"], "%Y-%m-%d %H:%M:%S") if cur else datetime.now()
    expire = base + timedelta(days=days)
    execute("INSERT INTO membership(user_id,level,days,source,expire_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (u["id"], "vip", days, "points", expire.strftime("%Y-%m-%d %H:%M:%S"), now_str()))
    change_points(u["id"], -cost, "vip_redeem", "积分兑换%s" % name)
    add_log("开通会员", user_id=u["id"], detail="%s（积分兑换 -%d）" % (name, cost))
    flash("开通成功！%s有效期至 %s，感谢支持" % (name, expire.strftime("%Y-%m-%d")), "ok")
    return redirect(url_for("vip_center"))


@app.route("/admin/users/<int:user_id>/vip", methods=["POST"])
@admin_required
def admin_user_vip(user_id):
    """管理员为用户开通会员（后台手动开通通道）"""
    u = query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    if not u:
        abort(404)
    try:
        days = int(request.form.get("days") or 30)
    except ValueError:
        days = 30
    days = min(max(days, 1), 3650)
    cur = user_vip(user_id)
    base = datetime.strptime(cur["expire_at"], "%Y-%m-%d %H:%M:%S") if cur else datetime.now()
    expire = base + timedelta(days=days)
    execute("INSERT INTO membership(user_id,level,days,source,expire_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, "vip", days, "admin", expire.strftime("%Y-%m-%d %H:%M:%S"), now_str()))
    add_log("管理员开通会员", user_id=user_id, admin_id=session["uid"],
            detail="%s %d天（至%s）" % (u["username"], days, expire.strftime("%Y-%m-%d")))
    flash("已为「%s」开通 %d 天会员" % (u["nickname"], days), "ok")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- V3.0 广告激励
@app.route("/ad")
@login_required
def ad_page():
    """广告激励页（Demo：模拟广告位展示）"""
    u = current_user()
    return render_template("ad.html", today_ads=today_ad_count(u["id"]),
                           ad_limit=AD_DAILY_LIMIT, ad_reward=AD_REWARD_POINTS,
                           points=user_points(u["id"]))


@app.route("/ad/claim", methods=["POST"])
@login_required
def ad_claim():
    """领取广告激励（服务端校验每日上限，防刷）"""
    u = current_user()
    cnt = today_ad_count(u["id"])
    if cnt >= AD_DAILY_LIMIT:
        flash("今日广告激励已达上限（%d次/天），明天再来吧" % AD_DAILY_LIMIT, "warn")
        return redirect(url_for("ad_page"))
    execute("INSERT INTO ad_rewards(user_id,reward_points,ad_scene,created_at) VALUES(?,?,?,?)",
            (u["id"], AD_REWARD_POINTS, "daily", now_str()))
    change_points(u["id"], AD_REWARD_POINTS, "ad_reward", "每日广告激励（第%d次）" % (cnt + 1))
    flash("感谢支持平台运营！+%d 积分已到账（今日第 %d/%d 次）"
          % (AD_REWARD_POINTS, cnt + 1, AD_DAILY_LIMIT), "ok")
    return redirect(url_for("ad_page"))


# ---------------------------------------------------------------- V3.0 作者付费咨询
@app.route("/doc/<int:doc_id>/consult", methods=["POST"])
@login_required
def post_consult(doc_id):
    """向资料作者发起付费咨询（积分支付，作者回答后结算，未回答可退款）"""
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d or d["status"] != "published":
        abort(404)
    u = current_user()
    if u["id"] == d["user_id"]:
        flash("不能向自己的资料发起咨询", "warn")
        return redirect(url_for("detail", doc_id=doc_id))
    question = (request.form.get("question") or "").strip()
    if len(question) < 5:
        flash("咨询问题至少5个字", "error")
        return redirect(url_for("detail", doc_id=doc_id))
    if len(question) > 300:
        flash("咨询问题过长（≤300字）", "error")
        return redirect(url_for("detail", doc_id=doc_id))
    pts = user_points(u["id"])
    if pts < CONSULT_PRICE:
        flash("积分不足：付费咨询需%d积分，当前%d积分。可通过上传资料、看广告获得积分"
              % (CONSULT_PRICE, pts), "error")
        return redirect(url_for("detail", doc_id=doc_id))
    cid = execute(
        "INSERT INTO consultations(document_id,from_user,to_user,question,price,status,created_at)"
        " VALUES(?,?,?,?,?,'pending',?)",
        (doc_id, u["id"], d["user_id"], question, CONSULT_PRICE, now_str()))
    change_points(u["id"], -CONSULT_PRICE, "consult_ask",
                  "向《%s》作者付费咨询#%d" % (d["title"][:20], cid))
    add_log("发起付费咨询", document_id=doc_id, user_id=u["id"],
            detail="咨询#%d -%d积分" % (cid, CONSULT_PRICE))
    flash("咨询已提交并支付%d积分。作者回答后积分结算给作者；未回答可在「我的贡献-我的咨询」关闭退款"
          % CONSULT_PRICE, "ok")
    return redirect(url_for("my"))


@app.route("/consult/<int:cid>/answer", methods=["POST"])
@login_required
def answer_consult(cid):
    """作者回答咨询 → 积分结算给作者"""
    cs = query("SELECT * FROM consultations WHERE id=?", (cid,), one=True)
    if not cs:
        abort(404)
    u = current_user()
    if u["id"] != cs["to_user"]:
        abort(403)
    if cs["status"] != "pending":
        flash("该咨询已处理", "warn")
        return redirect(url_for("my"))
    answer = (request.form.get("answer") or "").strip()
    if len(answer) < 5:
        flash("回答内容至少5个字", "error")
        return redirect(url_for("my"))
    if len(answer) > 500:
        flash("回答内容过长（≤500字）", "error")
        return redirect(url_for("my"))
    execute("UPDATE consultations SET answer=?, status='answered', answered_at=? WHERE id=?",
            (answer, now_str(), cid))
    change_points(u["id"], cs["price"], "consult_answer", "回答咨询#%d 获得酬劳" % cid)
    add_log("回答付费咨询", document_id=cs["document_id"], user_id=u["id"],
            detail="咨询#%d +%d积分" % (cid, cs["price"]))
    flash("回答成功，+%d 积分酬劳已到账" % cs["price"], "ok")
    return redirect(url_for("my"))


@app.route("/consult/<int:cid>/close", methods=["POST"])
@login_required
def close_consult(cid):
    """提问者关闭未回答的咨询 → 退款"""
    cs = query("SELECT * FROM consultations WHERE id=?", (cid,), one=True)
    if not cs:
        abort(404)
    u = current_user()
    if u["id"] != cs["from_user"]:
        abort(403)
    if cs["status"] != "pending":
        flash("仅未回答的咨询可关闭退款", "warn")
        return redirect(url_for("my"))
    execute("UPDATE consultations SET status='refunded', answered_at=? WHERE id=?",
            (now_str(), cid))
    change_points(u["id"], cs["price"], "consult_refund", "咨询#%d未回答，退款" % cid)
    flash("咨询已关闭，%d 积分已退回" % cs["price"], "ok")
    return redirect(url_for("my"))


# ---------------------------------------------------------------- V3.0 VIP批量下载
@app.route("/doc/<int:doc_id>/download")
@login_required
def doc_download(doc_id):
    """VIP权益：一键打包下载资料全部图片（ZIP）"""
    u = current_user()
    if not user_vip(u["id"]) and u["role"] != "admin":
        flash("打包下载是VIP会员权益，开通会员后可用", "warn")
        return redirect(url_for("vip_center"))
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d or d["status"] != "published":
        abort(404)
    images = doc_images(doc_id)
    if not images:
        flash("该资料暂无可下载的图片", "warn")
        return redirect(url_for("detail", doc_id=doc_id))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, img in enumerate(images):
            fp = os.path.join(UPLOAD_DIR, img["filename"])
            if os.path.exists(fp):
                ext = img["filename"].rsplit(".", 1)[-1]
                zf.write(fp, "第%d页.%s" % (i + 1, ext))
    buf.seek(0)
    add_log("打包下载", document_id=doc_id, user_id=u["id"], detail="%d张图片" % len(images))
    return send_file(buf, as_attachment=True, mimetype="application/zip",
                     download_name="资料%d_%s.zip" % (doc_id, d["title"][:20]))


# ---------------------------------------------------------------- V3.0 资料需求悬赏
@app.route("/bounty")
def bounty_hall():
    """悬赏大厅：发布需求 + 应征 + 采纳结算"""
    u = current_user()
    open_list = [dict(r) for r in query(
        """SELECT b.*, us.nickname,
           (SELECT COUNT(*) c FROM bounty_entries be WHERE be.bounty_id=b.id) AS entry_count
           FROM bounties b JOIN users us ON us.id=b.user_id
           WHERE b.status='open' ORDER BY b.created_at DESC LIMIT 100""")]
    done_list = [dict(r) for r in query(
        """SELECT b.*, us.nickname FROM bounties b JOIN users us ON us.id=b.user_id
           WHERE b.status IN ('fulfilled','closed') ORDER BY b.closed_at DESC LIMIT 20""")]
    my_pub_docs, my_bounties, my_entries = [], [], {}
    if u:
        my_pub_docs = query(
            "SELECT id,title FROM documents WHERE user_id=? AND status='published'"
            " ORDER BY published_at DESC", (u["id"],))
        my_bounties = [dict(r) for r in query(
            """SELECT b.*, (SELECT COUNT(*) c FROM bounty_entries be WHERE be.bounty_id=b.id) AS entry_count
               FROM bounties b WHERE b.user_id=? ORDER BY b.created_at DESC LIMIT 50""", (u["id"],))]
        if my_bounties:
            ids = ",".join(str(b["id"]) for b in my_bounties)
            for e in query(
                    """SELECT be.*, d.title AS doc_title, us.nickname FROM bounty_entries be
                       JOIN documents d ON d.id=be.document_id JOIN users us ON us.id=be.user_id
                       WHERE be.bounty_id IN (%s) ORDER BY be.created_at DESC""" % ids):
                my_entries.setdefault(e["bounty_id"], []).append(dict(e))
    return render_template("bounty.html", open_list=open_list, done_list=done_list,
                           my_pub_docs=my_pub_docs, my_bounties=my_bounties,
                           my_entries=my_entries, min_points=BOUNTY_MIN_POINTS,
                           points=user_points(u["id"]) if u else 0)


@app.route("/bounty/new", methods=["POST"])
@login_required
def bounty_new():
    """发布悬赏：扣除积分冻结，采纳应征后结算给对方，关闭则退回"""
    u = current_user()
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    province = (request.form.get("province") or "").strip()
    city = (request.form.get("city") or "").strip()
    org_name = (request.form.get("org_name") or "").strip()
    doc_type = (request.form.get("doc_type") or "").strip()
    try:
        reward = int(request.form.get("reward_points") or 0)
    except ValueError:
        reward = 0
    errs = []
    if len(title) < 5:
        errs.append("悬赏标题至少5个字")
    if len(title) > 60:
        errs.append("悬赏标题过长（≤60字）")
    if reward < BOUNTY_MIN_POINTS:
        errs.append("悬赏积分至少%d分" % BOUNTY_MIN_POINTS)
    if reward > 10000:
        errs.append("悬赏积分过高（≤10000）")
    pts = user_points(u["id"])
    if reward > pts:
        errs.append("积分不足：发布需%d积分，当前%d积分" % (reward, pts))
    if errs:
        for e in errs:
            flash(e, "error")
        return redirect(url_for("bounty_hall"))
    bid = execute(
        "INSERT INTO bounties(user_id,title,description,province,city,org_name,doc_type,"
        "reward_points,status,created_at) VALUES(?,?,?,?,?,?,?,?,'open',?)",
        (u["id"], title, description, province, city, org_name, doc_type, reward, now_str()))
    change_points(u["id"], -reward, "bounty_publish", "发布悬赏#%d《%s》" % (bid, title[:20]))
    add_log("发布悬赏", user_id=u["id"], detail="悬赏#%d 冻结%d积分" % (bid, reward))
    flash("悬赏发布成功！%d积分已冻结，采纳应征后结算给对方，关闭则退回" % reward, "ok")
    return redirect(url_for("bounty_hall"))


@app.route("/bounty/<int:bid>/enter", methods=["POST"])
@login_required
def bounty_enter(bid):
    """应征悬赏：关联本人已发布的资料"""
    b = query("SELECT * FROM bounties WHERE id=?", (bid,), one=True)
    if not b or b["status"] != "open":
        abort(404)
    u = current_user()
    if u["id"] == b["user_id"]:
        flash("不能应征自己发布的悬赏", "warn")
        return redirect(url_for("bounty_hall"))
    try:
        doc_id = int(request.form.get("document_id") or 0)
    except ValueError:
        doc_id = 0
    doc = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not doc or doc["user_id"] != u["id"] or doc["status"] != "published":
        flash("只能选择本人已发布的资料应征", "error")
        return redirect(url_for("bounty_hall"))
    exist = query("SELECT id FROM bounty_entries WHERE bounty_id=? AND user_id=? AND document_id=?",
                  (bid, u["id"], doc_id), one=True)
    if exist:
        flash("你已用该资料应征过此悬赏", "warn")
        return redirect(url_for("bounty_hall"))
    execute("INSERT INTO bounty_entries(bounty_id,user_id,document_id,status,created_at)"
            " VALUES(?,?,?,'pending',?)", (bid, u["id"], doc_id, now_str()))
    add_log("应征悬赏", document_id=doc_id, user_id=u["id"], detail="悬赏#%d" % bid)
    flash("应征成功！等待悬赏发布者确认", "ok")
    return redirect(url_for("bounty_hall"))


@app.route("/bounty/<int:bid>/accept/<int:eid>", methods=["POST"])
@login_required
def bounty_accept(bid, eid):
    """发布者采纳应征 → 悬赏积分结算给应征者，其余应征自动未采纳"""
    b = query("SELECT * FROM bounties WHERE id=?", (bid,), one=True)
    if not b or b["status"] != "open":
        abort(404)
    u = current_user()
    if u["id"] != b["user_id"]:
        abort(403)
    e = query("SELECT * FROM bounty_entries WHERE id=? AND bounty_id=? AND status='pending'",
              (eid, bid), one=True)
    if not e:
        flash("应征记录不存在或已处理", "error")
        return redirect(url_for("bounty_hall"))
    execute("UPDATE bounty_entries SET status='accepted' WHERE id=?", (eid,))
    execute("UPDATE bounty_entries SET status='rejected'"
            " WHERE bounty_id=? AND id!=? AND status='pending'", (bid, eid))
    execute("UPDATE bounties SET status='fulfilled', closed_at=? WHERE id=?", (now_str(), bid))
    change_points(e["user_id"], b["reward_points"], "bounty_reward",
                  "悬赏#%d《%s》被采纳" % (bid, b["title"][:20]))
    add_log("采纳悬赏", document_id=e["document_id"], user_id=u["id"],
            detail="悬赏#%d → 用户#%d +%d积分" % (bid, e["user_id"], b["reward_points"]))
    flash("已采纳！%d 积分已结算给应征者" % b["reward_points"], "ok")
    return redirect(url_for("bounty_hall"))


@app.route("/bounty/<int:bid>/close", methods=["POST"])
@login_required
def bounty_close(bid):
    """关闭悬赏 → 冻结积分退回发布者（发布者本人或管理员）"""
    b = query("SELECT * FROM bounties WHERE id=?", (bid,), one=True)
    if not b or b["status"] != "open":
        abort(404)
    u = current_user()
    if u["id"] != b["user_id"] and u["role"] != "admin":
        abort(403)
    execute("UPDATE bounties SET status='closed', closed_at=? WHERE id=?", (now_str(), bid))
    execute("UPDATE bounty_entries SET status='rejected' WHERE bounty_id=? AND status='pending'",
            (bid,))
    change_points(b["user_id"], b["reward_points"], "bounty_refund",
                  "悬赏#%d关闭，退回冻结积分" % bid)
    add_log("关闭悬赏", user_id=b["user_id"],
            admin_id=u["id"] if u["role"] == "admin" else None,
            detail="悬赏#%d +%d积分退回" % (bid, b["reward_points"]))
    flash("悬赏已关闭，%d 积分已退回发布者" % b["reward_points"], "ok")
    return redirect(url_for("bounty_hall"))


# ---------------------------------------------------------------- 搜索核心函数（供页面和AI共用）
def search_documents(kw="", province="", city="", industry="", doc_type="", org_name="",
                     business="", page=1, page_size=None):
    """
    统一的资料搜索函数，供页面路由和AI搜索调用
    返回: (items列表, total总数)
    """
    if page_size is None:
        page_size = PAGE_SIZE
    where = ["d.status='published'"]
    args = []
    if kw:
        like = "%" + kw + "%"
        where.append("(d.title LIKE ? OR d.tags LIKE ? OR d.business LIKE ? OR d.org_name LIKE ? OR d.source_org LIKE ? OR d.doc_type LIKE ? OR d.purpose LIKE ?)")
        args += [like] * 7
    if province:
        where.append("d.province LIKE ?")
        args.append("%" + province + "%")
    if city:
        where.append("d.city LIKE ?")
        args.append("%" + city + "%")
    if industry:
        where.append("d.industry_id IN (SELECT id FROM industries WHERE name LIKE ?)")
        args.append("%" + industry + "%")
    if doc_type:
        where.append("d.doc_type LIKE ?")
        args.append("%" + doc_type + "%")
    if org_name:
        where.append("d.org_name LIKE ?")
        args.append("%" + org_name + "%")
    if business:
        where.append("d.business LIKE ?")
        args.append("%" + business + "%")
    wsql = " AND ".join(where)
    total = query("SELECT COUNT(*) c FROM documents d WHERE " + wsql, tuple(args), one=True)["c"]
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    rows = query(
        "SELECT d.* FROM documents d WHERE %s ORDER BY d.published_at DESC LIMIT ? OFFSET ?" % wsql,
        tuple(args) + (page_size, (page - 1) * page_size))
    items = []
    for d in rows:
        it = fmt_row(d)
        it["cover"] = doc_cover(it["id"])
        items.append(it)
    return items, total


# ---------------------------------------------------------------- API预留接口
# 统一 JSON 接口层：为未来微信小程序端、AI服务、第三方程序调用预留
@app.route("/api/search")
def api_search():
    kw = (request.args.get("kw") or "").strip()
    province = (request.args.get("province") or "").strip()
    city = (request.args.get("city") or "").strip()
    industry = (request.args.get("industry") or "").strip()
    doc_type = (request.args.get("doc_type") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    where = ["d.status='published'"]
    args = []
    if kw:
        like = "%" + kw + "%"
        where.append("(d.title LIKE ? OR d.tags LIKE ? OR d.business LIKE ? OR d.org_name LIKE ? OR d.source_org LIKE ? OR d.doc_type LIKE ?)")
        args += [like] * 6
    for col, val in (("province", province), ("city", city), ("doc_type", doc_type)):
        if val:
            where.append("d.%s LIKE ?" % col)
            args.append("%" + val + "%")
    if industry:
        where.append("d.industry_id IN (SELECT id FROM industries WHERE name LIKE ?)")
        args.append("%" + industry + "%")
    wsql = " AND ".join(where)
    total = query("SELECT COUNT(*) c FROM documents d WHERE " + wsql, tuple(args), one=True)["c"]
    rows = query("SELECT d.* FROM documents d WHERE %s ORDER BY d.published_at DESC LIMIT ? OFFSET ?" % wsql,
                 tuple(args) + (PAGE_SIZE, (page - 1) * PAGE_SIZE))
    items = []
    for d in rows:
        it = fmt_row(d)
        it["cover"] = doc_cover(it["id"])
        items.append(it)
    return jsonify({"ok": True, "total": total, "page": page, "items": items})


@app.route("/api/doc/<int:doc_id>")
def api_doc(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        return jsonify({"ok": False, "message": "资料不存在"}), 404
    if d["status"] != "published":
        u = current_user()
        if not u or (u["id"] != d["user_id"] and u["role"] != "admin"):
            return jsonify({"ok": False, "message": "资料不存在或未公开"}), 404
    return jsonify({"ok": True, "item": fmt_row(d, with_images=True)})


@app.route("/api/categories")
def api_categories():
    return jsonify({"ok": True, "items": [
        {"id": r["id"], "name": r["name"], "is_open": bool(r["is_open"])}
        for r in query("SELECT * FROM industries ORDER BY sort")]})


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "ok": True,
        "published_docs": query("SELECT COUNT(*) c FROM documents WHERE status='published'", one=True)["c"],
        "pending_docs": query("SELECT COUNT(*) c FROM documents WHERE status='pending'", one=True)["c"],
        "contributors": query("SELECT COUNT(DISTINCT user_id) c FROM documents WHERE status='published'", one=True)["c"],
        "regions": query("SELECT COUNT(DISTINCT province) c FROM documents WHERE status='published'", one=True)["c"],
        "version": APP_VERSION,
    })


@app.route("/api/ai/search", methods=["GET", "POST"])
def api_ai_search():
    """AI智能搜索接口（V1.5）：自然语言解析→条件抽取→资料检索"""
    if request.method == "POST":
        user_query = (request.form.get("q") or request.form.get("query") or "").strip()
    else:
        user_query = (request.args.get("q") or request.args.get("query") or "").strip()
    if not user_query:
        return jsonify({"ok": False, "message": "请输入查询内容", "code": "EMPTY_QUERY"}), 400

    def _search(parsed):
        """供AI服务调用的搜索函数"""
        kw = " ".join(parsed.get("keywords", []))
        return search_documents(
            kw=kw,
            province=parsed.get("province", ""),
            city=parsed.get("city", ""),
            org_name=parsed.get("org_name", ""),
            business=parsed.get("business", ""),
            doc_type=parsed.get("doc_type", ""),
            page=1, page_size=10
        )

    result = ai_service.ai_search(user_query, _search)
    status_code = 200 if result["ok"] else 400
    return jsonify(result), status_code


# ---------------------------------------------------------------- V3.0 商业化API预留
@app.route("/api/vip/status")
def api_vip_status():
    """查询当前用户会员状态与积分（供小程序/第三方调用预留）"""
    u = current_user()
    if not u:
        return jsonify({"ok": False, "message": "请先登录"}), 401
    vip = user_vip(u["id"])
    return jsonify({"ok": True, "is_vip": bool(vip),
                    "expire_at": vip["expire_at"] if vip else None,
                    "points": user_points(u["id"])})


@app.route("/api/bounties")
def api_bounties():
    """进行中悬赏列表（对外开放）"""
    rows = query(
        """SELECT b.id, b.title, b.description, b.province, b.city, b.org_name, b.doc_type,
           b.reward_points, b.created_at, u.nickname,
           (SELECT COUNT(*) c FROM bounty_entries be WHERE be.bounty_id=b.id) AS entry_count
           FROM bounties b JOIN users u ON u.id=b.user_id
           WHERE b.status='open' ORDER BY b.created_at DESC LIMIT 50""")
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@app.route("/api/consultations")
@login_required
def api_consultations():
    """我的咨询列表（我提出的 + 待我回答的）"""
    u = current_user()
    asked = query("SELECT * FROM consultations WHERE from_user=? ORDER BY created_at DESC LIMIT 50",
                  (u["id"],))
    recv = query("SELECT * FROM consultations WHERE to_user=? ORDER BY created_at DESC LIMIT 50",
                 (u["id"],))
    return jsonify({"ok": True, "asked": [dict(r) for r in asked],
                    "received": [dict(r) for r in recv]})


# ---------------------------------------------------------------- 错误页
@app.errorhandler(404)
def page_404(_e):
    return render_template("error.html", code=404,
                           message="页面或资料不存在。可能资料未通过审核，或已被删除/下架。"), 404


@app.errorhandler(403)
def page_403(_e):
    return render_template("error.html", code=403,
                           message="没有权限执行该操作（权限已在服务端校验）。"), 403


@app.errorhandler(413)
def page_413(_e):
    return render_template("error.html", code=413,
                           message="上传内容总体积超过限制（100MB），请减少图片数量或压缩后重试。"), 413


@app.errorhandler(500)
def page_500(_e):
    return render_template("error.html", code=500,
                           message="服务器内部错误，请稍后重试或联系管理员。"), 500


# ---------------------------------------------------------------- 启动
if __name__ == "__main__":
    init_db()
    _port = int(os.environ.get("ZLBST_PORT", "8300"))
    _host = os.environ.get("ZLBST_HOST", "0.0.0.0")
    print("=" * 60)
    print("  资料百事通 %s  作者：%s（微信：%s）" % (APP_VERSION, APP_AUTHOR, APP_WECHAT))
    print("  程序大小：%.2f MB   最后保存：%s" % (APP_SIZE_MB, APP_LAST_SAVE.strftime("%Y-%m-%d %H:%M")))
    print("  访问地址：http://127.0.0.1:%d  （手机同局域网访问 http://本机IP:%d）" % (_port, _port))
    print("  管理后台：http://127.0.0.1:%d/admin  账号 admin / admin123" % _port)
    print("=" * 60)
    app.run(host=_host, port=_port, debug=False, threaded=True)
