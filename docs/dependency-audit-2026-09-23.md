# 依赖审计与许可证核对（2026-09-23）

**结论：生产运行时只有 4 个第三方包，生产上装的与 lock 逐字节一致，OSV 上 0 条公告，
四个都是宽松许可（与 AGPL-3.0 兼容）。**

这一轮之前它在清单里是「**没做**」——`handoff/DSH-NEXT.md` 的 C 组当时如实留着
「依赖审计与合成压测要另外论证」。压测那半仍然没做（见文末），审计这半做完了。

## 一、运行时依赖面（这场审计之所以便宜的真正原因）

`pilot_app/requirements.txt` 里**只声明了一个**第三方包：

```
cryptography>=43,<51      # AES-256-GCM 的密钥存储
```

它的注释解释了为什么这么小，而那句话本身就是一条设计约束：

> The web layer is Python standard library only: the previous FastAPI/uvicorn
> stack pulled a Starlette branch with open advisories and no compatible fixed
> release, so it was removed.

也就是说：**这个项目曾经有过依赖面，后来为了公告收窄回标准库了。**
`pilot_app/tests/test_dependency_audit.py::test_the_runtime_dependency_list_is_still_tiny`
是这条决定的棘轮：运行时依赖一旦多于一个，测试就红，逼着人把新包写进本文档。

## 二、锁定的版本 + 公告 + 许可证（2026-09-23 查）

| 包 | 版本 | OSV 公告 | 许可证 |
|---|---|---|---|
| `cryptography` | 50.0.1 | **0 条** | Apache-2.0 OR BSD-3-Clause |
| `cffi` | 2.0.0 | **0 条** | MIT-0 |
| `pycparser` | 2.23 | **0 条** | BSD-3-Clause |
| `typing_extensions` | 4.16.0 | **0 条** | PSF-2.0 |

**两个源，都是权威且只读的：**

* 公告 —— `https://api.osv.dev/v1/query`（按包名 + **确切版本**查，PyPI 生态）。
  逐包一次，四个包各返回 0 条。
* 许可证 —— `https://pypi.org/pypi/<包>/json` 的元数据（`license_expression` / 分类器）。

四者全是宽松许可 ⇒ **不与 AGPL-3.0 冲突**，也不引入 copyleft 传染。
（`cryptography` 内部静态链接了 OpenSSL 与 Rust 组件，上游用 Apache-2.0/BSD-3-Clause
双许可覆盖——这也是它元数据里写 `Apache-2.0 OR BSD-3-Clause` 的原因。）

## 三、生产上装的 = lock 里写的（无漂移）

`pilot_app/deploy_pilot.sh` 是用 `requirements.lock` 装的（`pip install -r`），
所以在生产上核一次「实际装了什么」就能看出有没有人手装过别的东西：

```
$ ssh … '/opt/cityu-mail-pilot/.venv/bin/pip list --format=freeze' | diff - <lock 去掉注释>
cffi==2.0.0
cryptography==50.0.1
pycparser==2.23
typing_extensions==4.16.0
→ 完全一致
```

Python 版本是 3.14（生产）／3.9（开发机）——两侧都跑同一套测试（CI 两个作业）。

## 三·五、一条可以重跑的命令（而不是一句「我查过了」）

**结论文档不会自己变旧，包会。** 所以判据做成可重跑的：

```bash
.venv-pilot/bin/python -m pilot_app.manage check-deps
#   依赖：pilot_app/requirements.lock
#     lock 里 4 行，其中 4 个能按确切版本查（其余是注释或不带 == 的行，不猜）
#     cffi                 2.0.0        ✅
#     cryptography         50.0.1       ✅
#     pycparser            2.23         ✅
#     typing_extensions    4.16.0       ✅
#   结论：4 个包、按确切版本查过，0 条公告。
```

新模块 `pilot_app/deps.py` + `manage check-deps`，三条设计边界：

* **只问 lock 里的确切版本**（不是"最新版安不安全"）——要回答的是「**我们装的那一份**」；
* **「没查到公告」与「没查成」严格分开**：网络不通、OSV 5xx、响应形状变了（连
  `vulns: null` 也算）一律进 `failed`、**非零退出**，绝不当成干净。
  这条与 `budget` 那条「读不到账就放行」**方向相反**，因为代价不对称：
  漏报一条真公告的代价远大于多让人跑一次命令；
* **只读**：URL 过 `security.validate_outbound_https_url`（必须 https、必须公网、不许带凭据），
  不改版本、不自动升级——自动升级是运维决定，不是审计的产物。

## 四、这一套怎么防漂移

新增 `pilot_app/tests/test_dependency_audit.py`（14 条）：

1. **运行时依赖只有一个**（棘轮，带理由）；
2. **lock 里的版本就是被审计过的那些** —— 升级任何一个包，这条红；
3. **许可证全是宽松的**（读本文档里那张人工维护的表——许可证没法自动核，但它在树里有一个位置）；
4. **lock 完全钉住**（不许 `>=`/`~=`，否则生产装到哪个版本是运气）；
5. **本文档存在、且写明两个来源与「0 条」**（结论必须可复核，不能只留在某个人的记忆里）；
6. 另外 9 条钉 `deps.py` 与那条命令的行为：lock 解析只认钉死的行、
   `{}` = 干净而 `vulns: null` = **没查成**、查不动 ⇒ `failed` 且**非零退出**、
   出站闸门照用（`deps` 不许成为绕过 `validate_outbound_https_url` 的第二个出口）、
   真有公告时退出码非零并指向本文档。

## 五、没做的那一半（如实）

**合成压测**没做，理由与上一轮相同：这台生产只有 2 核，压测本身会影响真实用户。
2026-09-22 曾经把这一半**切给宿舍机**做过一次（`docs/loadtest-2026-09-22.md`：
75400 个请求、非 2xx = 0、吞吐天花板 ≈270 rps），结论是 **Web 层不是瓶颈**。
真正没压过的是**主服务那台盒子的推理槽**（2 槽、家宽上行、租来的隧道）——
那个要压只能压那台机器，而它同时在给真实用户出报告。**没有做，也不建议在没有
维护方在场的情况下做。**
