"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { CheckIcon } from "./icons";

export function BetaSignupForm() {
  const [submitted, setSubmitted] = useState(false);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitted(true);
  }

  if (submitted) {
    return (
      <div className="signup-success" role="status">
        <span className="success-icon">
          <CheckIcon />
        </span>
        <h2>申请已收到</h2>
        <p>我们会在 24 小时内把授权指引发到你的校园邮箱。</p>
        <Link className="pill-ghost" href="/">
          返回首页
        </Link>
      </div>
    );
  }

  return (
    <form className="signup-form" onSubmit={handleSubmit}>
      <div className="signup-form-row">
        <label htmlFor="signup-email">校园邮箱</label>
        <span className="meta">仅支持 CityU 校园邮箱</span>
      </div>
      <input
        id="signup-email"
        name="email"
        type="email"
        placeholder="name@cityu.edu.hk"
        required
      />
      <label htmlFor="signup-name">怎么称呼你</label>
      <input id="signup-name" name="name" type="text" placeholder="例如：Alex" required />
      <label htmlFor="signup-note">你最想解决什么问题？</label>
      <textarea
        id="signup-note"
        name="note"
        rows={4}
        placeholder="可选，告诉我们你最常漏看的邮件类型"
      />
      <button className="btn-done signup-submit" type="submit">
        提交内测申请
        <span className="icon-wrap" aria-hidden="true">
          <CheckIcon />
        </span>
      </button>
      <p className="signup-note">
        当前为前端演示，提交信息不会发送到服务器。
      </p>
    </form>
  );
}
