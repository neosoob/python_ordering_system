# Python 点餐系统

一个基于 `Flask + SQLite` 的响应式点餐系统，适配手机端和电脑端。

## 功能

- 默认进入用户端查看菜单
- 匿名访客可直接加入购物车并下单
- “我的订单”展示当前浏览器下过的订单
- 管理员登录后台后可维护菜单分类和菜品信息
- 管理员可修改店名，前台和后台都会展示
- 菜品信息支持图片、名称、介绍、价格、上下架状态
- 数据库存储使用 SQLite

## 默认管理员账号

- 用户名：`admin`
- 密码：`admin123`

## 运行

```bash
pip install -r requirements.txt
python app.py
```

访问地址：

- 用户端：`http://127.0.0.1:7878`
- 管理员登录：`http://127.0.0.1:7878/admin/login`

## 测试

```bash
python -m unittest tests/test_app.py
```
