import os
from io import BytesIO
import unittest

from app import create_app, get_db


class OrderingSystemTestCase(unittest.TestCase):
    def setUp(self):
        self.test_db = os.path.join(os.getcwd(), "instance", "test.db")
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.test_db,
                "UPLOAD_FOLDER": os.path.join(os.getcwd(), "static", "uploads"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)

    def login_admin(self):
        response = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

    def create_order(self):
        with self.app.app_context():
            dish = get_db().execute(
                "SELECT id FROM dishes ORDER BY id LIMIT 1"
            ).fetchone()

        self.client.post(
            f"/cart/add/{dish['id']}",
            data={"quantity": "2", "next": "/"},
            follow_redirects=True,
        )
        self.client.post(
            "/checkout",
            data={"customer_name": "test-customer"},
            follow_redirects=True,
        )

    def test_homepage_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<section class="hero-panel">', response.data)

    def test_guest_can_place_order(self):
        self.create_order()
        with self.app.app_context():
            order_count = get_db().execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(order_count, 1)

    def test_admin_can_update_store_name(self):
        self.login_admin()

        update_response = self.client.post(
            "/admin/store",
            data={"store_name": "deep-night-kitchen"},
            follow_redirects=True,
        )
        self.assertEqual(update_response.status_code, 200)
        with self.app.app_context():
            store_name = get_db().execute(
                "SELECT value FROM settings WHERE key = ?",
                ("store_name",),
            ).fetchone()[0]
        self.assertEqual(store_name, "deep-night-kitchen")

    def test_demo_dishes_are_not_reseeded_after_delete(self):
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM dishes")
            db.commit()

        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.test_db,
                "UPLOAD_FOLDER": os.path.join(os.getcwd(), "static", "uploads"),
            }
        )

        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
        self.assertEqual(count, 0)

    def test_admin_dish_create_rejects_invalid_filename(self):
        self.login_admin()
        with self.app.app_context():
            category_id = get_db().execute(
                "SELECT id FROM categories ORDER BY id LIMIT 1"
            ).fetchone()[0]

        response = self.client.post(
            "/admin/dishes/create",
            data={
                "name": "upload-check",
                "category_id": str(category_id),
                "price": "9.90",
                "description": "invalid filename case",
                "is_active": "on",
                "image": (BytesIO(b"fake-image"), "###"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            count = get_db().execute(
                "SELECT COUNT(*) FROM dishes WHERE name = ?",
                ("upload-check",),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_restart_does_not_add_extra_categories(self):
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM dishes")
            db.execute("DELETE FROM categories")
            db.execute("INSERT INTO categories (name, sort_order) VALUES (?, ?)", ("Sushi", 1))
            db.commit()
            before = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]

        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.test_db,
                "UPLOAD_FOLDER": os.path.join(os.getcwd(), "static", "uploads"),
            }
        )

        with self.app.app_context():
            after = get_db().execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        self.assertEqual(before, after)

    def test_admin_can_delete_order(self):
        self.create_order()
        self.login_admin()
        with self.app.app_context():
            order_id = get_db().execute(
                "SELECT id FROM orders ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        response = self.client.post(
            f"/admin/orders/{order_id}/delete",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            order_count = get_db().execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            item_count = get_db().execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
        self.assertEqual(order_count, 0)
        self.assertEqual(item_count, 0)

    def test_admin_can_delete_order_item(self):
        with self.app.app_context():
            dishes = get_db().execute(
                "SELECT id FROM dishes ORDER BY id LIMIT 2"
            ).fetchall()

        self.client.post(
            f"/cart/add/{dishes[0]['id']}",
            data={"quantity": "1", "next": "/"},
            follow_redirects=True,
        )
        self.client.post(
            f"/cart/add/{dishes[1]['id']}",
            data={"quantity": "1", "next": "/"},
            follow_redirects=True,
        )
        self.client.post(
            "/checkout",
            data={"customer_name": "test-customer"},
            follow_redirects=True,
        )
        self.login_admin()
        with self.app.app_context():
            db = get_db()
            order_id = db.execute(
                "SELECT id FROM orders ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            item_id = db.execute(
                "SELECT id FROM order_items WHERE order_id = ? ORDER BY id LIMIT 1",
                (order_id,),
            ).fetchone()[0]

        response = self.client.post(
            f"/admin/orders/{order_id}/items/{item_id}/delete",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            db = get_db()
            remaining_items = db.execute(
                "SELECT COUNT(*) FROM order_items WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
            remaining_order = db.execute(
                "SELECT total_amount FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            expected_total = db.execute(
                "SELECT COALESCE(SUM(subtotal), 0) FROM order_items WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
        self.assertEqual(remaining_items, 1)
        self.assertIsNotNone(remaining_order)
        self.assertEqual(remaining_order[0], expected_total)


if __name__ == "__main__":
    unittest.main()
