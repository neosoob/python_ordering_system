import os
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

    def test_homepage_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("宫保鸡丁饭".encode("utf-8"), response.data)

    def test_guest_can_place_order(self):
        with self.app.app_context():
            dish = get_db().execute(
                "SELECT id FROM dishes ORDER BY id LIMIT 1"
            ).fetchone()

        add_response = self.client.post(
            f"/cart/add/{dish['id']}",
            data={"quantity": "2", "next": "/"},
            follow_redirects=True,
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertIn("已加入购物车".encode("utf-8"), add_response.data)

        checkout_response = self.client.post(
            "/checkout",
            data={"customer_name": "测试顾客"},
            follow_redirects=True,
        )
        self.assertEqual(checkout_response.status_code, 200)
        self.assertIn("订单提交成功".encode("utf-8"), checkout_response.data)
        self.assertIn("测试顾客".encode("utf-8"), checkout_response.data)

    def test_admin_can_update_store_name(self):
        login_response = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=True,
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("管理后台".encode("utf-8"), login_response.data)

        update_response = self.client.post(
            "/admin/store",
            data={"store_name": "深夜食堂"},
            follow_redirects=True,
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertIn("深夜食堂".encode("utf-8"), update_response.data)


if __name__ == "__main__":
    unittest.main()
