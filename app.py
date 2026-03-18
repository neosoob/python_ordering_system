import os
import sqlite3
import uuid
from functools import wraps

from flask import Flask, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
DEFAULT_IMAGE_PATH = "demo/default-dish.svg"
DEFAULT_STORE_NAME = "点餐系统"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
ORDER_STATUS_SUBMITTED = "已提交"
BAD_STORE_NAMES = {"", "Ordering System", "????", "点餐系统?", "鐐归绯荤粺"}
DEMO_CATEGORY_MAP = {
    "Featured": "热销推荐",
    "Rice": "米饭主食",
    "Drinks": "饮品甜点",
}
DEMO_DISH_MAP = {
    "Signature Salmon Bowl": {
        "name": "招牌三文鱼饭",
        "description": "厚切三文鱼配寿司米、温泉蛋与特制酱汁。",
    },
    "Teriyaki Chicken Rice": {
        "name": "照烧鸡排饭",
        "description": "鸡排现煎现烤，搭配照烧汁与时令配菜。",
    },
    "Yuzu Sparkling Water": {
        "name": "柚子气泡水",
        "description": "清爽微气泡口感，适合搭配海鲜与炸物。",
    },
    "Matcha Milk": {
        "name": "抹茶牛乳",
        "description": "抹茶与鲜奶调和，口感顺滑细腻。",
    },
}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    image_path TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_token TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    total_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT '已提交',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    dish_id INTEGER NOT NULL,
    dish_name TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    subtotal REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (dish_id) REFERENCES dishes(id) ON DELETE RESTRICT
);
"""


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        DATABASE=os.path.join(app.instance_path, "ordering.db"),
        UPLOAD_FOLDER=os.path.join(app.static_folder, "uploads"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )

    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    @app.teardown_appcontext
    def close_db(error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.before_request
    def load_runtime_state():
        ensure_visitor_token()
        g.admin = None
        admin_id = session.get("admin_id")
        if admin_id is not None:
            g.admin = get_db().execute(
                "SELECT * FROM admins WHERE id = ?",
                (admin_id,),
            ).fetchone()

    @app.context_processor
    def inject_layout_state():
        cart = session.get("cart", {})
        return {
            "cart_count": sum(int(quantity) for quantity in cart.values()),
            "store_name": get_setting("store_name", DEFAULT_STORE_NAME),
        }

    @app.template_filter("currency")
    def currency_filter(value):
        return "{:.2f}".format(float(value))

    @app.errorhandler(413)
    def file_too_large(error):
        flash("图片过大，请上传 4MB 以内的图片。", "warning")
        return redirect(request.referrer or url_for("admin_dashboard"))

    @app.route("/")
    def index():
        db = get_db()
        categories = db.execute(
            """
            SELECT c.*, COUNT(d.id) AS active_dish_count
            FROM categories c
            LEFT JOIN dishes d
              ON d.category_id = c.id AND d.is_active = 1
            GROUP BY c.id
            ORDER BY c.sort_order, c.id
            """
        ).fetchall()
        dishes = db.execute(
            """
            SELECT d.*, c.name AS category_name
            FROM dishes d
            JOIN categories c ON c.id = d.category_id
            WHERE d.is_active = 1
            ORDER BY c.sort_order, d.id DESC
            """
        ).fetchall()
        grouped_menu = build_grouped_menu(categories, dishes)
        cart_items, cart_total = get_cart_items()
        order_count = db.execute(
            "SELECT COUNT(*) AS count FROM orders WHERE visitor_token = ?",
            (g.visitor_token,),
        ).fetchone()["count"]
        return render_template(
            "index.html",
            grouped_menu=grouped_menu,
            cart_items=cart_items,
            cart_total=cart_total,
            order_count=order_count,
        )

    @app.route("/cart")
    def cart():
        cart_items, cart_total = get_cart_items()
        return render_template("cart.html", cart_items=cart_items, cart_total=cart_total)

    @app.route("/orders")
    def orders():
        db = get_db()
        orders = db.execute(
            """
            SELECT *
            FROM orders
            WHERE visitor_token = ?
            ORDER BY created_at DESC, id DESC
            """,
            (g.visitor_token,),
        ).fetchall()
        order_items = db.execute(
            """
            SELECT oi.*, o.id AS order_id
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.visitor_token = ?
            ORDER BY o.created_at DESC, oi.id ASC
            """,
            (g.visitor_token,),
        ).fetchall()
        item_map = {}
        for item in order_items:
            item_map.setdefault(item["order_id"], []).append(item)
        order_cards = [
            {"order": order, "items": item_map.get(order["id"], [])}
            for order in orders
        ]
        return render_template("orders.html", order_cards=order_cards)

    @app.route("/cart/add/<int:dish_id>", methods=("POST",))
    def add_to_cart(dish_id):
        dish = get_db().execute(
            "SELECT * FROM dishes WHERE id = ? AND is_active = 1",
            (dish_id,),
        ).fetchone()
        if dish is None:
            if wants_json_response():
                return jsonify({"ok": False, "message": "菜品不存在或已下架。"}), 404
            flash("菜品不存在或已下架。", "warning")
            return redirect(url_for("index"))

        quantity = parse_quantity(request.form.get("quantity", "1"))
        cart = session.get("cart", {})
        dish_key = str(dish_id)
        cart[dish_key] = min(cart.get(dish_key, 0) + quantity, 99)
        session["cart"] = cart
        if wants_json_response():
            return jsonify(
                {
                    "ok": True,
                    "message": "已加入购物车。",
                    "cart": build_cart_payload(),
                }
            )
        flash("已加入购物车。", "success")
        return redirect(get_safe_next("index"))

    @app.route("/cart/update/<int:dish_id>", methods=("POST",))
    def update_cart(dish_id):
        quantity = parse_quantity(request.form.get("quantity", "1"))
        cart = session.get("cart", {})
        dish_key = str(dish_id)
        message = "购物车已更新。"
        if quantity <= 0:
            cart.pop(dish_key, None)
            message = "已从购物车移除菜品。"
        else:
            cart[dish_key] = min(quantity, 99)
        session["cart"] = cart
        if wants_json_response():
            return jsonify({"ok": True, "message": message, "cart": build_cart_payload()})
        flash(message, "success")
        return redirect(get_safe_next("cart"))

    @app.route("/cart/remove/<int:dish_id>", methods=("POST",))
    def remove_from_cart(dish_id):
        cart = session.get("cart", {})
        cart.pop(str(dish_id), None)
        session["cart"] = cart
        if wants_json_response():
            return jsonify(
                {
                    "ok": True,
                    "message": "已从购物车移除菜品。",
                    "cart": build_cart_payload(),
                }
            )
        flash("已从购物车移除菜品。", "success")
        return redirect(get_safe_next("cart"))

    @app.route("/checkout", methods=("POST",))
    def checkout():
        cart_items, cart_total = get_cart_items()
        if not cart_items:
            flash("购物车为空，请先添加菜品。", "warning")
            return redirect(url_for("index"))

        customer_name = clean_text(request.form.get("customer_name", "").strip())
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO orders (visitor_token, customer_name, total_amount, status)
            VALUES (?, ?, ?, ?)
            """,
            (g.visitor_token, customer_name, cart_total, ORDER_STATUS_SUBMITTED),
        )
        order_id = cursor.lastrowid
        for item in cart_items:
            db.execute(
                """
                INSERT INTO order_items (order_id, dish_id, dish_name, price, quantity, subtotal)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["dish"]["id"],
                    clean_text(item["dish"]["name"]),
                    item["dish"]["price"],
                    item["quantity"],
                    item["subtotal"],
                ),
            )
        db.commit()
        session["cart"] = {}
        flash("订单提交成功。", "success")
        return redirect(url_for("orders"))

    @app.route("/admin")
    def admin_root():
        if g.admin:
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("admin_login"))

    @app.route("/admin/login", methods=("GET", "POST"))
    def admin_login():
        if g.admin:
            return redirect(url_for("admin_dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            admin = get_db().execute(
                "SELECT * FROM admins WHERE username = ?",
                (username,),
            ).fetchone()
            if admin and check_password_hash(admin["password_hash"], password):
                session["admin_id"] = admin["id"]
                flash("管理员登录成功。", "success")
                return redirect(url_for("admin_dashboard"))
            flash("管理员账号或密码错误。", "warning")

        return render_template("admin_login.html")

    @app.route("/admin/logout")
    @admin_required
    def admin_logout():
        session.pop("admin_id", None)
        flash("已退出管理员后台。", "success")
        return redirect(url_for("index"))

    @app.route("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        db = get_db()
        categories = db.execute(
            """
            SELECT c.*, COUNT(d.id) AS dish_count
            FROM categories c
            LEFT JOIN dishes d ON d.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order, c.id
            """
        ).fetchall()
        dishes = db.execute(
            """
            SELECT d.*, c.name AS category_name
            FROM dishes d
            JOIN categories c ON c.id = d.category_id
            ORDER BY c.sort_order, d.id DESC
            """
        ).fetchall()
        category_count = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        dish_count = db.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
        order_count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        admin_orders = load_admin_orders(db)
        return render_template(
            "admin_dashboard.html",
            categories=categories,
            dishes=dishes,
            category_count=category_count,
            dish_count=dish_count,
            order_count=order_count,
            admin_orders=admin_orders,
        )

    @app.route("/admin/store", methods=("POST",))
    @admin_required
    def admin_store_update():
        store_name = clean_text(request.form.get("store_name", "").strip())
        if not store_name:
            flash("店名不能为空。", "warning")
            return redirect(url_for("admin_dashboard"))
        set_setting("store_name", store_name)
        flash("店名更新成功。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/orders/<int:order_id>/delete", methods=("POST",))
    @admin_required
    def admin_order_delete(order_id):
        db = get_db()
        order = db.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            flash("订单不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        db.commit()
        flash("订单删除成功。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/orders/<int:order_id>/items/<int:item_id>/delete", methods=("POST",))
    @admin_required
    def admin_order_item_delete(order_id, item_id):
        db = get_db()
        order = db.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            flash("订单不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        item = db.execute(
            "SELECT id FROM order_items WHERE id = ? AND order_id = ?",
            (item_id, order_id),
        ).fetchone()
        if item is None:
            flash("订单菜品不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        db.execute("DELETE FROM order_items WHERE id = ?", (item_id,))
        remaining_count = db.execute(
            "SELECT COUNT(*) FROM order_items WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        if remaining_count == 0:
            db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            db.commit()
            flash("订单中已无菜品，整单已删除。", "success")
            return redirect(url_for("admin_dashboard"))

        refresh_order_total(db, order_id)
        db.commit()
        flash("订单菜品删除成功。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/categories/create", methods=("GET", "POST"))
    @admin_required
    def admin_category_create():
        if request.method == "POST":
            name = clean_text(request.form.get("name", "").strip())
            sort_order = parse_integer(request.form.get("sort_order", "0"), default=0)
            if not name:
                flash("分类名称不能为空。", "warning")
            elif category_name_exists(name):
                flash("分类名称已存在。", "warning")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                    (name, sort_order),
                )
                db.commit()
                flash("分类创建成功。", "success")
                return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin_category_form.html",
            category=None,
            form_title="新增分类",
            submit_label="创建分类",
        )

    @app.route("/admin/categories/<int:category_id>/edit", methods=("GET", "POST"))
    @admin_required
    def admin_category_edit(category_id):
        db = get_db()
        category = db.execute(
            "SELECT * FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()
        if category is None:
            flash("分类不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        if request.method == "POST":
            name = clean_text(request.form.get("name", "").strip())
            sort_order = parse_integer(request.form.get("sort_order", "0"), default=0)
            if not name:
                flash("分类名称不能为空。", "warning")
            elif category_name_exists(name, exclude_id=category_id):
                flash("分类名称已存在。", "warning")
            else:
                db.execute(
                    "UPDATE categories SET name = ?, sort_order = ? WHERE id = ?",
                    (name, sort_order, category_id),
                )
                db.commit()
                flash("分类更新成功。", "success")
                return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin_category_form.html",
            category=category,
            form_title="编辑分类",
            submit_label="保存分类",
        )

    @app.route("/admin/categories/<int:category_id>/delete", methods=("POST",))
    @admin_required
    def admin_category_delete(category_id):
        db = get_db()
        category = db.execute(
            "SELECT * FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()
        if category is None:
            flash("分类不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        dish_count = db.execute(
            "SELECT COUNT(*) FROM dishes WHERE category_id = ?",
            (category_id,),
        ).fetchone()[0]
        if dish_count:
            flash("该分类下仍有菜品，无法删除。", "warning")
            return redirect(url_for("admin_dashboard"))

        db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        db.commit()
        flash("分类删除成功。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/dishes/create", methods=("GET", "POST"))
    @admin_required
    def admin_dish_create():
        categories = load_categories()
        if not categories:
            flash("请先创建菜品分类。", "warning")
            return redirect(url_for("admin_dashboard"))

        if request.method == "POST":
            form_data, error = validate_dish_form(categories)
            if error:
                flash(error, "warning")
            else:
                db = get_db()
                db.execute(
                    """
                    INSERT INTO dishes (category_id, name, description, price, image_path, is_active)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        form_data["category_id"],
                        form_data["name"],
                        form_data["description"],
                        form_data["price"],
                        form_data["image_path"],
                        form_data["is_active"],
                    ),
                )
                db.commit()
                flash("菜品创建成功。", "success")
                return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin_dish_form.html",
            dish=None,
            categories=categories,
            form_title="新增菜品",
            submit_label="创建菜品",
        )

    @app.route("/admin/dishes/<int:dish_id>/edit", methods=("GET", "POST"))
    @admin_required
    def admin_dish_edit(dish_id):
        db = get_db()
        dish = db.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        if dish is None:
            flash("菜品不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        categories = load_categories()
        if request.method == "POST":
            form_data, error = validate_dish_form(categories, existing_dish=dish)
            if error:
                flash(error, "warning")
            else:
                db.execute(
                    """
                    UPDATE dishes
                    SET category_id = ?, name = ?, description = ?, price = ?, image_path = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        form_data["category_id"],
                        form_data["name"],
                        form_data["description"],
                        form_data["price"],
                        form_data["image_path"],
                        form_data["is_active"],
                        dish_id,
                    ),
                )
                db.commit()
                flash("菜品更新成功。", "success")
                return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin_dish_form.html",
            dish=dish,
            categories=categories,
            form_title="编辑菜品",
            submit_label="保存菜品",
        )

    @app.route("/admin/dishes/<int:dish_id>/delete", methods=("POST",))
    @admin_required
    def admin_dish_delete(dish_id):
        db = get_db()
        dish = db.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        if dish is None:
            flash("菜品不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        db.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
        db.commit()
        flash("菜品删除成功。", "success")
        return redirect(url_for("admin_dashboard"))

    with app.app_context():
        init_database()

    return app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_database():
    db = get_db()
    db.executescript(SCHEMA_SQL)
    ensure_admin_account(db)
    ensure_setting(db, "store_name", DEFAULT_STORE_NAME)
    ensure_setting(db, "demo_data_seeded", "0")
    migrate_existing_data(db)
    seed_demo_data(db)
    db.commit()


def ensure_admin_account(db):
    admin = db.execute(
        "SELECT id FROM admins WHERE username = ?",
        (DEFAULT_ADMIN_USERNAME,),
    ).fetchone()
    if admin is None:
        db.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )


def ensure_setting(db, key, value):
    existing = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if existing is None:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))


def migrate_existing_data(db):
    store_row = db.execute("SELECT value FROM settings WHERE key = 'store_name'").fetchone()
    if store_row and store_row[0] in BAD_STORE_NAMES:
        db.execute(
            "UPDATE settings SET value = ? WHERE key = 'store_name'",
            (DEFAULT_STORE_NAME,),
        )
    elif store_row:
        repaired_store = clean_text(store_row[0])
        if repaired_store != store_row[0]:
            db.execute(
                "UPDATE settings SET value = ? WHERE key = 'store_name'",
                (repaired_store,),
            )

    for row in db.execute("SELECT id, status, customer_name FROM orders").fetchall():
        status = ORDER_STATUS_SUBMITTED
        customer_name = clean_text(row["customer_name"])
        if status != row["status"] or customer_name != row["customer_name"]:
            db.execute(
                "UPDATE orders SET status = ?, customer_name = ? WHERE id = ?",
                (status, customer_name, row["id"]),
            )

    for row in db.execute("SELECT id, name FROM categories").fetchall():
        name = clean_text(row["name"])
        name = DEMO_CATEGORY_MAP.get(name, name)
        if is_placeholder_name(name):
            dish_count = db.execute(
                "SELECT COUNT(*) FROM dishes WHERE category_id = ?",
                (row["id"],),
            ).fetchone()[0]
            if dish_count == 0:
                db.execute("DELETE FROM categories WHERE id = ?", (row["id"],))
                continue
        if name != row["name"]:
            db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, row["id"]))

    category_name_to_id = {
        row["name"]: row["id"]
        for row in db.execute("SELECT id, name FROM categories").fetchall()
    }

    for row in db.execute("SELECT id, category_id, name, description FROM dishes").fetchall():
        name = clean_text(row["name"])
        description = clean_text(row["description"])
        category_id = row["category_id"]
        if name in DEMO_DISH_MAP:
            mapped = DEMO_DISH_MAP[name]
            name = mapped["name"]
            description = mapped["description"]
        category_row = db.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()
        if category_row:
            new_category_name = DEMO_CATEGORY_MAP.get(category_row["name"])
            if new_category_name and new_category_name in category_name_to_id:
                category_id = category_name_to_id[new_category_name]
        if (
            name != row["name"]
            or description != row["description"]
            or category_id != row["category_id"]
        ):
            db.execute(
                "UPDATE dishes SET category_id = ?, name = ?, description = ? WHERE id = ?",
                (category_id, name, description, row["id"]),
            )

    for row in db.execute("SELECT id, dish_name FROM order_items").fetchall():
        repaired_name = clean_text(row["dish_name"])
        if repaired_name != row["dish_name"]:
            db.execute(
                "UPDATE order_items SET dish_name = ? WHERE id = ?",
                (repaired_name, row["id"]),
            )


def seed_demo_data(db):
    seeded = db.execute(
        "SELECT value FROM settings WHERE key = 'demo_data_seeded'"
    ).fetchone()[0]
    category_count = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    dish_count = db.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
    if seeded == "1" or category_count > 0 or dish_count > 0:
        if seeded != "1":
            db.execute(
                "UPDATE settings SET value = '1' WHERE key = 'demo_data_seeded'"
            )
        return

    categories = [
        ("热销推荐", 1),
        ("米饭主食", 2),
        ("饮品甜点", 3),
    ]
    for name, sort_order in categories:
        db.execute(
            "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
            (name, sort_order),
        )

    category_map = {
        row["name"]: row["id"]
        for row in db.execute("SELECT id, name FROM categories").fetchall()
    }
    demo_dishes = [
        ("热销推荐", "招牌三文鱼饭", "厚切三文鱼配寿司米、温泉蛋与特制酱汁。", 48.0),
        ("米饭主食", "照烧鸡排饭", "鸡排现煎现烤，搭配照烧汁与时令配菜。", 36.0),
        ("饮品甜点", "柚子气泡水", "清爽微气泡口感，适合搭配海鲜与炸物。", 12.0),
        ("饮品甜点", "抹茶牛乳", "抹茶与鲜奶调和，口感顺滑细腻。", 18.0),
    ]
    for category_name, name, description, price in demo_dishes:
        db.execute(
            """
            INSERT INTO dishes (category_id, name, description, price, image_path, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                category_map[category_name],
                name,
                description,
                price,
                DEFAULT_IMAGE_PATH,
            ),
        )
    db.execute("UPDATE settings SET value = '1' WHERE key = 'demo_data_seeded'")


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if not g.admin:
            flash("请先登录管理员账号。", "warning")
            return redirect(url_for("admin_login"))
        return view(**kwargs)

    return wrapped_view


def ensure_visitor_token():
    token = session.get("visitor_token")
    if not token:
        token = uuid.uuid4().hex
        session["visitor_token"] = token
    g.visitor_token = token


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.commit()


def load_admin_orders(db):
    orders = db.execute(
        """
        SELECT o.*,
               COUNT(oi.id) AS item_count
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        GROUP BY o.id
        ORDER BY o.created_at DESC, o.id DESC
        """
    ).fetchall()
    items = db.execute(
        """
        SELECT *
        FROM order_items
        ORDER BY order_id DESC, id ASC
        """
    ).fetchall()
    item_map = {}
    for item in items:
        item_map.setdefault(item["order_id"], []).append(item)
    return [{"order": order, "items": item_map.get(order["id"], [])} for order in orders]


def refresh_order_total(db, order_id):
    total = db.execute(
        "SELECT COALESCE(SUM(subtotal), 0) FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE orders SET total_amount = ? WHERE id = ?",
        (round(float(total), 2), order_id),
    )


def build_cart_payload():
    cart_items, cart_total = get_cart_items()
    return {
        "count": sum(item["quantity"] for item in cart_items),
        "distinct_count": len(cart_items),
        "total": round(float(cart_total), 2),
        "items": [
            {
                "dish_id": item["dish"]["id"],
                "name": item["dish"]["name"],
                "description": item["dish"]["description"],
                "image_path": item["dish"]["image_path"] or DEFAULT_IMAGE_PATH,
                "price": round(float(item["dish"]["price"]), 2),
                "quantity": item["quantity"],
                "subtotal": round(float(item["subtotal"]), 2),
            }
            for item in cart_items
        ],
    }


def wants_json_response():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def build_grouped_menu(categories, dishes):
    dish_map = {}
    for dish in dishes:
        dish_map.setdefault(dish["category_id"], []).append(dish)
    return [
        {"category": category, "dishes": dish_map.get(category["id"], [])}
        for category in categories
        if dish_map.get(category["id"])
    ]


def get_cart_items():
    cart = session.get("cart", {})
    dish_ids = [int(dish_id) for dish_id in cart.keys() if str(dish_id).isdigit()]
    if not dish_ids:
        return [], 0.0

    placeholders = ",".join("?" for _ in dish_ids)
    dishes = get_db().execute(
        f"SELECT * FROM dishes WHERE id IN ({placeholders}) AND is_active = 1",
        tuple(dish_ids),
    ).fetchall()
    dish_map = {dish["id"]: dish for dish in dishes}
    items = []
    total = 0.0
    cleaned_cart = {}
    for dish_id_str, quantity in cart.items():
        try:
            dish_id = int(dish_id_str)
            quantity_value = max(1, min(int(quantity), 99))
        except (TypeError, ValueError):
            continue
        dish = dish_map.get(dish_id)
        if dish is None:
            continue
        subtotal = float(dish["price"]) * quantity_value
        items.append({"dish": dish, "quantity": quantity_value, "subtotal": subtotal})
        total += subtotal
        cleaned_cart[dish_id_str] = quantity_value
    if cleaned_cart != cart:
        session["cart"] = cleaned_cart
    return items, total


def parse_quantity(raw_value):
    try:
        return max(0, min(int(raw_value), 99))
    except (TypeError, ValueError):
        return 1


def parse_integer(raw_value, default=0):
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def parse_price(raw_value):
    try:
        value = round(float(raw_value), 2)
        if value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError):
        return None


def get_safe_next(default_endpoint):
    next_value = request.form.get("next") or request.args.get("next")
    if next_value and next_value.startswith("/") and not next_value.startswith("//"):
        return next_value
    return url_for(default_endpoint)


def load_categories():
    return get_db().execute(
        "SELECT * FROM categories ORDER BY sort_order, id"
    ).fetchall()


def category_name_exists(name, exclude_id=None):
    query = "SELECT id FROM categories WHERE name = ?"
    params = [name]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return get_db().execute(query, tuple(params)).fetchone() is not None


def validate_dish_form(categories, existing_dish=None):
    category_ids = {category["id"] for category in categories}
    name = clean_text(request.form.get("name", "").strip())
    description = clean_text(request.form.get("description", "").strip())
    category_id = parse_integer(request.form.get("category_id"), default=-1)
    price = parse_price(request.form.get("price"))
    is_active = 1 if request.form.get("is_active") else 0

    if not name:
        return None, "菜品名称不能为空。"
    if category_id not in category_ids:
        return None, "请选择有效的菜品分类。"
    if price is None:
        return None, "请输入有效价格。"
    if not description:
        return None, "菜品介绍不能为空。"

    image_file = request.files.get("image")
    image_path = existing_dish["image_path"] if existing_dish else DEFAULT_IMAGE_PATH
    if image_file and image_file.filename:
        image_path, error = save_uploaded_image(image_file)
        if error:
            return None, error

    return {
        "category_id": category_id,
        "name": name,
        "description": description,
        "price": price,
        "image_path": image_path or DEFAULT_IMAGE_PATH,
        "is_active": is_active,
    }, None


def save_uploaded_image(image_file):
    filename = secure_filename(image_file.filename or "")
    if not filename:
        return None, "图片文件名无效，请重新选择图片。"
    if "." not in filename:
        return None, "图片文件格式不正确，请上传常见图片格式。"
    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        return None, "仅支持 png、jpg、jpeg、gif、webp、svg 格式图片。"

    unique_name = f"{uuid.uuid4().hex}.{extension}"
    relative_path = os.path.join("uploads", unique_name).replace("\\", "/")
    absolute_path = os.path.join(current_app.static_folder, relative_path)
    image_file.save(absolute_path)
    return relative_path, None


def is_placeholder_name(value):
    stripped = (value or "").strip()
    if not stripped:
        return True
    return all(char in {"?", "？", "�"} for char in stripped)


def clean_text(value):
    if value is None:
        return ""
    text = str(value)
    candidate = text.strip()
    if not candidate:
        return ""
    repaired = repair_mojibake(candidate)
    return repaired.strip()


def repair_mojibake(text):
    best = text
    seen = {text}
    while True:
        candidate = try_repair_utf8_latin1(best)
        if not candidate or candidate in seen:
            break
        if score_text(candidate) < score_text(best):
            break
        seen.add(candidate)
        best = candidate
    return best


def try_repair_utf8_latin1(text):
    try:
        return text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def score_text(text):
    score = 0
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF:
            score += 4
        elif 0x3040 <= code <= 0x30FF:
            score += 2
        elif char.isalnum() or char in " -_./:&()[]{}+@#%*，。！？、￥":
            score += 1
        elif char in "ÃÂÐ¤¦¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿":
            score -= 3
        else:
            score -= 1
    return score


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7878, debug=True)
