"use client";

import { useEffect, useMemo, useState } from "react";
import { mails, urgencyLevels, type Urgency } from "../data/mail";
import { CheckIcon } from "./icons";
import { Shell } from "./design-system";

type Filter = "all" | `${Urgency}`;
type Sort = "priority" | "recent";

export function InboxDemo() {
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("priority");
  const [current, setCurrent] = useState(mails[0].id);
  const [done, setDone] = useState<string[]>([]);

  useEffect(() => {
    let timer: number | undefined;
    try {
      const stored = JSON.parse(window.localStorage.getItem("mailpilot.done.v1") ?? "[]");
      if (Array.isArray(stored)) {
        timer = window.setTimeout(() => setDone(stored), 0);
      }
    } catch {
      // Invalid storage is treated as an empty completion list.
    }
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("mailpilot.done.v1", JSON.stringify(done));
    } catch {
      // Private browsing can disable localStorage; the in-memory state still works.
    }
  }, [done]);

  const visible = useMemo(() => {
    const filtered = mails.filter(
      (mail) => filter === "all" || String(mail.urgency) === filter,
    );
    return filtered.sort((a, b) =>
      sort === "priority" ? b.urgency - a.urgency : a.id.localeCompare(b.id),
    );
  }, [filter, sort]);

  const selected = mails.find((mail) => mail.id === current) ?? visible[0] ?? mails[0];
  const isDone = done.includes(selected.id);
  const pending = mails.filter((mail) => !done.includes(mail.id)).length;
  const hot = mails.filter((mail) => mail.urgency === 3 && !done.includes(mail.id)).length;

  function selectFilter(value: Filter) {
    setFilter(value);
    const next = mails.filter((mail) => value === "all" || String(mail.urgency) === value);
    if (next.length && !next.some((mail) => mail.id === current)) setCurrent(next[0].id);
  }

  function toggleDone() {
    setDone((items) =>
      items.includes(selected.id)
        ? items.filter((id) => id !== selected.id)
        : [...items, selected.id],
    );
  }

  return (
    <div className="split rv-soft" style={{ "--i": 2 } as React.CSSProperties}>
      <Shell coreStyle={{ padding: 24 }}>
        <div className="app-head">
          <div className="head-left">
            <h3>收件箱</h3>
            <span className="meta">
              {visible.length} 封 · 待办 {pending} · 紧急 {hot}
            </span>
          </div>
          <div className="seg" role="group" aria-label="排序方式">
            {([
              ["priority", "紧急优先"],
              ["recent", "按时间"],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={sort === value}
                onClick={() => setSort(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="chips" role="group" aria-label="按紧急程度筛选">
          <button
            className="chip"
            type="button"
            aria-pressed={filter === "all"}
            onClick={() => selectFilter("all")}
          >
            全部
          </button>
          {([
            ["3", "紧急", "hi"],
            ["2", "较急", "mid"],
            ["1", "常规", "low"],
          ] as const).map(([value, label, tone]) => (
            <button
              className="chip"
              type="button"
              aria-pressed={filter === value}
              onClick={() => selectFilter(value)}
              key={value}
            >
              <i className={`dot ${tone}`} aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
        <ul className="maillist">
          {visible.map((mail) => {
            const level = urgencyLevels[mail.urgency];
            const completed = done.includes(mail.id);
            return (
              <li key={mail.id}>
                <button
                  className={`mail ${mail.seen ? "seen" : ""} ${completed ? "done" : ""}`}
                  type="button"
                  aria-current={selected.id === mail.id}
                  onClick={() => setCurrent(mail.id)}
                >
                  <span className="top">
                    <span className="from">
                      <span className="mono-badge" aria-hidden="true">
                        {mail.badge}
                      </span>
                      <span className="sender">{mail.from}</span>
                    </span>
                    <span className="time">{mail.time}</span>
                  </span>
                  <span className="subject">{mail.subject}</span>
                  <span className="row2">
                    <span className={`lv ${completed ? "ok" : level.tone}`}>
                      <i aria-hidden="true" />
                      {completed ? "已完成" : level.name}
                    </span>
                    <span className="time">截止 {mail.due}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        {!visible.length ? <p className="list-empty">这个紧急程度暂时没有邮件。</p> : null}
      </Shell>

      <Shell coreStyle={{ padding: 24 }}>
        <div className="detail">
          <h3>{selected.subject}</h3>
          <div className="fromline">
            <span className="mono-badge" aria-hidden="true">
              {selected.badge}
            </span>
            <span>{selected.from}</span>
            <span className="time">{selected.time}</span>
          </div>
          <UrgencyPanel urgency={selected.urgency} why={selected.why} />
          <p className="body">{selected.body}</p>
          <div className="meta" style={{ marginTop: 22 }}>
            要办的事 · {selected.steps.length} 步
          </div>
          <ul className="steps">
            {selected.steps.map((step, index) => (
              <li key={step}>
                <span className="n" aria-hidden="true">
                  {index + 1}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ul>
          <div className="detail-actions">
            <button
              className={`btn-done ${isDone ? "is-done" : ""}`}
              type="button"
              onClick={toggleDone}
            >
              <CheckIcon />
              {isDone ? "已完成" : "标记为已办"}
            </button>
            <button className="btn-soft" type="button">
              加入日历
            </button>
            <button className="btn-soft" type="button">
              稍后提醒
            </button>
          </div>
        </div>
      </Shell>
    </div>
  );
}

function UrgencyPanel({ urgency, why }: { urgency: Urgency; why: string[] }) {
  const level = urgencyLevels[urgency];
  return (
    <div className={`urg ${level.tone}`}>
      <div className="urg-head">
        <strong>{level.name}</strong>
        <span className="meter" role="img" aria-label={`紧急程度 ${level.name}`}>
          {[1, 2, 3].map((step) => (
            <i className={step <= urgency ? "on" : ""} key={step} />
          ))}
        </span>
        <span className="meta">{level.hint}</span>
      </div>
      <div className="urg-why">
        <span className="meta">判定依据</span>
        <ul>
          {why.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
