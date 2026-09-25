"""共享夹具：**建号 + 授权**，不再拿保留地址走开放注册。

为什么会有这个文件（2026-09-26）。`web.register()` 现在拒绝所有命中
`INFE_PILOT_ADMIN_EMAILS` 的地址：**403、永不建号**。那是 GPT 外部审计报的 P1 ——
开放注册不验证邮箱归属，谁抢在主人之前用那个地址注册，谁当场就是管理员
（`_is_admin()` 只看邮箱字符串）。此前很多模块的夹具正是这么拿管理员会话的，
所以闸门一上就红。**红得对**：那就是攻击者的做法，测试不该再演一遍。

这里的路子与 `manage create-admin --apply` 是同一条：先把账号建出来
（`Database.create_user`），再授权（`Database.grant_admin`），最后走**真的登录端点**
拿会话。它**没有绕过任何闸门** —— `/api/auth/register` 碰都不碰，权限走的是
v0.34.0 起就有的后台「授权」那条路；闸门本身由 `test_web.ReservedAdminAddressTests`
正向 + 反向盯着。

用法（`db` 就是本模块里那个 `pilot_app.web.db`，或自己的 `Database`）::

    from pilot_app.tests import admin_fixture

    def _admin(self) -> Client:
        client = Client(self.base)
        return admin_fixture.admin_session(db, client, "boss@example.com")

普通用户的夹具**不要**改：`POST /api/auth/register` 对非保留地址仍然完全开放，
那是产品行为，走 HTTP 才是对的。
"""

PASSWORD = "a-long-enough-password"


def _address(email: str) -> str:
    return str(email).strip().lower()


def create_account(database, email: str, password: str = PASSWORD) -> dict:
    """建一个已知密码的账号，直接写库（注册端点不参与）。返回账号行。"""
    from pilot_app.security import hash_password, verify_password

    address = _address(email)
    existing = database.find_user_for_login(address)
    if existing is not None:
        # 复用已有账号，但保证密码就是调用方说的那个：否则下一步登录会 401，
        # 而那看起来像"夹具坏了"，不像"这条断言不成立"。
        if not verify_password(password, existing["password_hash"]):
            database.set_password(existing["id"], hash_password(password))
        return existing
    return database.create_user(address, hash_password(password), "")


def create_admin(database, email: str, password: str = PASSWORD) -> dict:
    """建号 + 授权，等价于 `manage create-admin --apply` 那两件事。返回账号行。"""
    address = _address(email)
    create_account(database, address, password)
    database.grant_admin(address)   # 只给**已有**账号授权：这个方法自己就是这么要求的
    user = database.find_user_for_login(address)
    assert user is not None, f"{address} 建号之后查不到"
    return user


def sign_in(client, email: str, password: str = PASSWORD):
    """走真的 `POST /api/auth/login` 拿会话，返回同一个 client。

    各家 `Client` 的形状不一样：有的只有 `request(method, path, payload)`，
    有的另包了 `post`；返回值有的二元组、有的三元组。这里两种都认，只取前两个。
    """
    payload = {"email": _address(email), "password": password}
    poster = getattr(client, "post", None)
    response = (poster("/api/auth/login", payload) if poster is not None
                else client.request("POST", "/api/auth/login", payload))
    status, body = response[:2]
    if status != 200:
        raise AssertionError(f"登录 {_address(email)} 失败：{status} {body}")
    return client


def admin_session(database, client, email: str, password: str = PASSWORD):
    """建号 + 授权 + 登录，一把交回带管理员会话的 client。"""
    create_admin(database, email, password)
    return sign_in(client, email, password)
