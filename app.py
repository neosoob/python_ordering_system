import os
import sqlite3
import uuid
from functools import wraps

from flask import (
    Flask,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
DEFAULT_IMAGE_PATH = "demo/default-dish.svg"
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
            "store_name": get_setting("store_name", "食刻点餐"),
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
        return render_template(
            "cart.html",
            cart_items=cart_items,
            cart_total=cart_total,
        )

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
            flash("菜品不存在或已下架。", "warning")
            return redirect(url_for("index"))

        quantity = parse_quantity(request.form.get("quantity", "1"))
        cart = session.get("cart", {})
        dish_key = str(dish_id)
        cart[dish_key] = min(cart.get(dish_key, 0) + quantity, 99)
        session["cart"] = cart
        flash("已加入购物车。", "success")
        return redirect(get_safe_next("index"))

    @app.route("/cart/update/<int:dish_id>", methods=("POST",))
    def update_cart(dish_id):
        quantity = parse_quantity(request.form.get("quantity", "1"))
        cart = session.get("cart", {})
        dish_key = str(dish_id)
        if quantity <= 0:
            cart.pop(dish_key, None)
            flash("已从购物车移除菜品。", "success")
        else:
            cart[dish_key] = min(quantity, 99)
            flash("购物车已更新。", "success")
        session["cart"] = cart
        return redirect(get_safe_next("cart"))

    @app.route("/cart/remove/<int:dish_id>", methods=("POST",))
    def remove_from_cart(dish_id):
        cart = session.get("cart", {})
        cart.pop(str(dish_id), None)
        session["cart"] = cart
        flash("已从购物车移除菜品。", "success")
        return redirect(get_safe_next("cart"))

    @app.route("/checkout", methods=("POST",))
    def checkout():
        cart_items, cart_total = get_cart_items()
        if not cart_items:
            flash("购物车为空，请先添加菜品。", "warning")
            return redirect(url_for("index"))

        customer_name = request.form.get("customer_name", "").strip()
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO orders (visitor_token, customer_name, total_amount, status)
            VALUES (?, ?, ?, ?)
            """,
            (g.visitor_token, customer_name, cart_total, "已提交"),
        )
        order_id = cursor.lastrowid
        for item in cart_items:
            dish = item["dish"]
            db.execute(
                """
                INSERT INTO order_items (
                    order_id, dish_id, dish_name, price, quantity, subtotal
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    dish["id"],
                    dish["name"],
                    float(dish["price"]),
                    item["quantity"],
                    item["subtotal"],
                ),
            )
        db.commit()
        session["cart"] = {}
        flash("订单提交成功，可在“我的订单”里查看当前设备下过的订单。", "success")
        return redirect(url_for("orders"))

    @app.route("/admin/login", methods=("GET", "POST"))
    def admin_login():
        if g.admin is not None:
            return redirect(url_for("admin_dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            admin = get_db().execute(
                "SELECT * FROM admins WHERE username = ?",
                (username,),
            ).fetchone()
            if admin is None or not check_password_hash(admin["password_hash"], password):
                flash("管理员账号或密码错误。", "warning")
            else:
                session["admin_id"] = admin["id"]
                flash("管理员登录成功。", "success")
                return redirect(url_for("admin_dashboard"))

        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("admin_id", None)
        flash("已退出管理员后台。", "success")
        return redirect(url_for("index"))

    @app.route("/admin")
    @admin_required
    def admin_dashboard():
        db = get_db()
        category_count = db.execute("SELECT COUNT(*) AS count FROM categories").fetchone()["count"]
        dish_count = db.execute("SELECT COUNT(*) AS count FROM dishes").fetchone()["count"]
        order_count = db.execute("SELECT COUNT(*) AS count FROM orders").fetchone()["count"]
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
        recent_orders = db.execute(
            """
            SELECT id, customer_name, total_amount, status, created_at
            FROM orders
            ORDER BY created_at DESC, id DESC
            LIMIT 8
            """
        ).fetchall()
        return render_template(
            "admin_dashboard.html",
            categories=categories,
            dishes=dishes,
            recent_orders=recent_orders,
            category_count=category_count,
            dish_count=dish_count,
            order_count=order_count,
        )

    @app.route("/admin/store", methods=("POST",))
    @admin_required
    def admin_store_update():
        store_name = request.form.get("store_name", "").strip()
        if not store_name:
            flash("店名不能为空。", "warning")
        else:
            set_setting("store_name", store_name)
            flash("店名更新成功。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/categories/create", methods=("GET", "POST"))
    @admin_required
    def admin_category_create():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            sort_order = parse_sort_order(request.form.get("sort_order", "0"))

            if not name:
                flash("分类名称不能为空。", "warning")
            else:
                try:
                    db = get_db()
                    db.execute(
                        "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                        (name, sort_order),
                    )
                    db.commit()
                    flash("分类创建成功。", "success")
                    return redirect(url_for("admin_dashboard"))
                except sqlite3.IntegrityError:
                    flash("分类名称已存在。", "warning")

        return render_template(
            "admin_category_form.html",
            category=None,
            form_title="新增分类",
            submit_label="保存分类",
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
            name = request.form.get("name", "").strip()
            sort_order = parse_sort_order(request.form.get("sort_order", "0"))

            if not name:
                flash("分类名称不能为空。", "warning")
            else:
                try:
                    db.execute(
                        """
                        UPDATE categories
                        SET name = ?, sort_order = ?
                        WHERE id = ?
                        """,
                        (name, sort_order, category_id),
                    )
                    db.commit()
                    flash("分类更新成功。", "success")
                    return redirect(url_for("admin_dashboard"))
                except sqlite3.IntegrityError:
                    flash("分类名称已存在。", "warning")

        return render_template(
            "admin_category_form.html",
            category=category,
            form_title="编辑分类",
            submit_label="更新分类",
        )

    @app.route("/admin/categories/<int:category_id>/delete", methods=("POST",))
    @admin_required
    def admin_category_delete(category_id):
        db = get_db()
        dish_count = db.execute(
            "SELECT COUNT(*) AS count FROM dishes WHERE category_id = ?",
            (category_id,),
        ).fetchone()["count"]
        if dish_count:
            flash("该分类下仍有菜品，请先处理菜品。", "warning")
        else:
            db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
            db.commit()
            flash("分类已删除。", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/dishes/create", methods=("GET", "POST"))
    @admin_required
    def admin_dish_create():
        db = get_db()
        categories = db.execute(
            "SELECT * FROM categories ORDER BY sort_order, id"
        ).fetchall()
        if not categories:
            flash("请先创建分类，再新增菜品。", "warning")
            return redirect(url_for("admin_category_create"))

        if request.method == "POST":
            form_data, error = validate_dish_form(request, categories)
            if error is None:
                image_path = DEFAULT_IMAGE_PATH
                image_file = request.files.get("image")
                if image_file and image_file.filename:
                    try:
                        image_path = save_uploaded_image(image_file)
                    except ValueError as exc:
                        error = str(exc)

                if error is None:
                    db.execute(
                        """
                        INSERT INTO dishes (
                            category_id, name, description, price, image_path, is_active
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            form_data["category_id"],
                            form_data["name"],
                            form_data["description"],
                            form_data["price"],
                            image_path,
                            form_data["is_active"],
                        ),
                    )
                    db.commit()
                    flash("菜品创建成功。", "success")
                    return redirect(url_for("admin_dashboard"))

            flash(error, "warning")

        return render_template(
            "admin_dish_form.html",
            dish=None,
            categories=categories,
            form_title="新增菜品",
            submit_label="保存菜品",
        )

    @app.route("/admin/dishes/<int:dish_id>/edit", methods=("GET", "POST"))
    @admin_required
    def admin_dish_edit(dish_id):
        db = get_db()
        categories = db.execute(
            "SELECT * FROM categories ORDER BY sort_order, id"
        ).fetchall()
        dish = db.execute(
            "SELECT * FROM dishes WHERE id = ?",
            (dish_id,),
        ).fetchone()
        if dish is None:
            flash("菜品不存在。", "warning")
            return redirect(url_for("admin_dashboard"))

        if request.method == "POST":
            form_data, error = validate_dish_form(request, categories)
            image_path = dish["image_path"] or DEFAULT_IMAGE_PATH

            image_file = request.files.get("image")
            if error is None and image_file and image_file.filename:
                try:
                    new_image_path = save_uploaded_image(image_file)
                    delete_uploaded_image(dish["image_path"])
                    image_path = new_image_path
                except ValueError as exc:
                    error = str(exc)

            if error is None:
                db.execute(
                    """
                    UPDATE dishes
                    SET category_id = ?,
                        name = ?,
                        description = ?,
                        price = ?,
                        image_path = ?,
                        is_active = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        form_data["category_id"],
                        form_data["name"],
                        form_data["description"],
                        form_data["price"],
                        image_path,
                        form_data["is_active"],
                        dish_id,
                    ),
                )
                db.commit()
                flash("菜品更新成功。", "success")
                return redirect(url_for("admin_dashboard"))

            flash(error, "warning")

        return render_template(
            "admin_dish_form.html",
            dish=dish,
            categories=categories,
            form_title="编辑菜品",
            submit_label="更新菜品",
        )

    @app.route("/admin/dishes/<int:dish_id>/delete", methods=("POST",))
    @admin_required
    def admin_dish_delete(dish_id):
        db = get_db()
        dish = db.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        if dish is None:
            flash("菜品不存在。", "warning")
        else:
            delete_uploaded_image(dish["image_path"])
            db.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
            db.commit()
            flash("菜品已删除。", "success")
        return redirect(url_for("admin_dashboard"))

    with app.app_context():
        init_db()
        seed_data()

    return app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def ensure_visitor_token():
    if "visitor_token" not in session:
        session["visitor_token"] = uuid.uuid4().hex
    g.visitor_token = session["visitor_token"]


def init_db():
    db = get_db()
    db.executescript(SCHEMA_SQL)
    db.commit()


def seed_data():
    db = get_db()
    admin = db.execute(
        "SELECT id FROM admins WHERE username = ?",
        ("admin",),
    ).fetchone()
    if admin is None:
        db.execute(
            "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
            ("admin", generate_password_hash("admin123")),
        )
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("store_name", "食刻点餐"),
    )

    categories = [
        ("热销推荐", 1),
        ("米饭主食", 2),
        ("饮品甜点", 3),
    ]
    for name, sort_order in categories:
        db.execute(
            "INSERT OR IGNORE INTO categories (name, sort_order) VALUES (?, ?)",
            (name, sort_order),
        )
    db.commit()

    dish_count = db.execute("SELECT COUNT(*) AS count FROM dishes").fetchone()["count"]
    if dish_count == 0:
        category_map = {
            row["name"]: row["id"]
            for row in db.execute("SELECT id, name FROM categories").fetchall()
        }
        demo_dishes = [
            (
                category_map["热销推荐"],
                "宫保鸡丁饭",
                "鸡丁、花生和小米椒一起爆香，适合作为工作日午餐。",
                24.0,
                DEFAULT_IMAGE_PATH,
                1,
            ),
            (
                category_map["热销推荐"],
                "黑椒牛肉意面",
                "黑椒酱汁包裹牛肉片和意面，味道偏浓郁。",
                29.0,
                DEFAULT_IMAGE_PATH,
                1,
            ),
            (
                category_map["米饭主食"],
                "扬州炒饭",
                "火腿、鸡蛋、青豆与米饭同炒，口感清爽。",
                18.0,
                DEFAULT_IMAGE_PATH,
                1,
            ),
            (
                category_map["饮品甜点"],
                "柠檬红茶",
                "现泡红茶加入新鲜柠檬片，适合搭配重口味菜品。",
                12.0,
                DEFAULT_IMAGE_PATH,
                1,
            ),
        ]
        db.executemany(
            """
            INSERT INTO dishes (
                category_id, name, description, price, image_path, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            demo_dishes,
        )
        db.commit()


def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.admin is None:
            flash("请先登录管理员账号。", "warning")
            return redirect(url_for("admin_login"))
        return view(**kwargs)

    return wrapped_view


def get_safe_next(default_endpoint):
    target = request.form.get("next") or request.args.get("next")
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for(default_endpoint)


def get_setting(key, default_value=""):
    row = get_db().execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default_value
    return row["value"]


def set_setting(key, value):
    db = get_db()
    db.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.commit()


def parse_quantity(value):
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = 1
    return max(quantity, 0)


def parse_sort_order(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_grouped_menu(categories, dishes):
    dish_map = {}
    for dish in dishes:
        dish_map.setdefault(dish["category_id"], []).append(dish)

    groups = []
    for category in categories:
        category_dishes = dish_map.get(category["id"], [])
        if category_dishes:
            groups.append({"category": category, "dishes": category_dishes})
    return groups


def get_cart_items():
    cart = session.get("cart", {})
    if not cart:
        return [], 0.0

    dish_ids = []
    for raw_id in cart.keys():
        try:
            dish_ids.append(int(raw_id))
        except ValueError:
            continue

    if not dish_ids:
        return [], 0.0

    placeholders = ",".join("?" for _ in dish_ids)
    dishes = get_db().execute(
        f"""
        SELECT d.*, c.name AS category_name
        FROM dishes d
        JOIN categories c ON c.id = d.category_id
        WHERE d.id IN ({placeholders}) AND d.is_active = 1
        """,
        tuple(dish_ids),
    ).fetchall()
    dish_map = {str(dish["id"]): dish for dish in dishes}

    items = []
    total = 0.0
    stale_ids = []
    for dish_id, quantity in cart.items():
        dish = dish_map.get(str(dish_id))
        if dish is None:
            stale_ids.append(str(dish_id))
            continue

        quantity = max(int(quantity), 1)
        subtotal = float(dish["price"]) * quantity
        total += subtotal
        items.append(
            {
                "dish": dish,
                "quantity": quantity,
                "subtotal": subtotal,
            }
        )

    if stale_ids:
        for stale_id in stale_ids:
            cart.pop(stale_id, None)
        session["cart"] = cart

    return items, total


def validate_dish_form(req, categories):
    name = req.form.get("name", "").strip()
    description = req.form.get("description", "").strip()
    category_id = req.form.get("category_id", "").strip()
    price_raw = req.form.get("price", "").strip()
    is_active = 1 if req.form.get("is_active") == "on" else 0

    if not name:
        return None, "菜品名称不能为空。"
    if not description:
        return None, "菜品介绍不能为空。"

    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return None, "请选择正确的菜品分类。"

    category_ids = {category["id"] for category in categories}
    if category_id not in category_ids:
        return None, "请选择正确的菜品分类。"

    try:
        price = round(float(price_raw), 2)
    except (TypeError, ValueError):
        return None, "请输入正确的价格。"

    if price <= 0:
        return None, "价格必须大于 0。"

    return {
        "name": name,
        "description": description,
        "category_id": category_id,
        "price": price,
        "is_active": is_active,
    }, None


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_image(file_storage):
    if file_storage is None or not file_storage.filename:
        raise ValueError("请选择图片后再上传。")
    if not allowed_file(file_storage.filename):
        raise ValueError("图片格式不支持，仅支持 png/jpg/jpeg/gif/webp/svg。")

    filename = secure_filename(file_storage.filename)
    extension = filename.rsplit(".", 1)[1].lower()
    unique_name = "{}.{}".format(uuid.uuid4().hex, extension)
    relative_path = os.path.join("uploads", unique_name).replace("\\", "/")
    save_path = os.path.join(current_app.static_folder, relative_path.replace("/", os.sep))
    file_storage.save(save_path)
    return relative_path


def delete_uploaded_image(image_path):
    if not image_path or not image_path.startswith("uploads/"):
        return
    file_path = os.path.join(current_app.static_folder, image_path.replace("/", os.sep))
    if os.path.exists(file_path):
        os.remove(file_path)


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=7878)
