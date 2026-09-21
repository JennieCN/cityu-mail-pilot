"use client";

import { useEffect, useRef, useState } from "react";
import { ButtonLink, GlassLayer } from "./design-system";
import { EnvelopeIcon } from "./icons";

const sections = [
  { id: "why", label: "它做什么" },
  { id: "inbox", label: "收件箱演示" },
  { id: "privacy", label: "隐私说明" },
  { id: "faq", label: "常见问题" },
];

export function SiteNav() {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    document.body.classList.toggle("menu-open", open);
    return () => document.body.classList.remove("menu-open");
  }, [open]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }

    const wide = window.matchMedia("(min-width:769px)");
    const closeOnWide = () => {
      if (wide.matches) setOpen(false);
    };

    document.addEventListener("keydown", onKeyDown);
    wide.addEventListener("change", closeOnWide);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      wide.removeEventListener("change", closeOnWide);
    };
  }, []);

  return (
    <div className="topbar" data-od-id="topbar">
      <div className="nav-shell" data-od-id="topnav">
        <GlassLayer />
        <header className="nav">
          <a className="brand" href="#top" data-od-id="brand">
            <span className="mark" aria-hidden="true">
              <EnvelopeIcon />
            </span>
            CityU Mail
          </a>
          <nav className="nav-links" aria-label="页面章节">
            <ul>
              {sections.map((section) => (
                <li key={section.id}>
                  <a href={`#${section.id}`} data-nav={section.id}>
                    <i className="fg" aria-hidden="true" />
                    <span className="lb">{section.label}</span>
                  </a>
                </li>
              ))}
            </ul>
          </nav>
          <ButtonLink
            variant="ghost"
            showIcon={false}
            className="nav-cta"
            href="/beta-signup"
            data-od-id="nav-cta"
          >
            内测名额
          </ButtonLink>
          <button
            ref={buttonRef}
            className="menu-btn"
            type="button"
            aria-expanded={open}
            aria-controls="nav-sheet"
            aria-label={open ? "关闭章节菜单" : "打开章节菜单"}
            data-od-id="menu-btn"
            onClick={() => setOpen((value) => !value)}
          >
            <span className="bars" aria-hidden="true">
              <i />
              <i />
            </span>
          </button>
        </header>
        <div className="nav-progress" aria-hidden="true">
          <i id="nav-progress-bar" />
        </div>
      </div>

      <div
        className={`nav-sheet ${open ? "is-open" : ""}`}
        id="nav-sheet"
        data-od-id="nav-sheet"
      >
        <GlassLayer />
        <div className="sheet-inner">
          <span className="sheet-label" id="nav-sheet-label">
            页面章节
          </span>
          <ul aria-labelledby="nav-sheet-label">
            {sections.map((section, index) => (
              <li key={section.id}>
                <a
                  className="row"
                  href={`#${section.id}`}
                  data-nav={section.id}
                  style={{ "--i": index } as React.CSSProperties}
                  onClick={() => setOpen(false)}
                >
                  <span className="idx">{String(index + 1).padStart(2, "0")}</span>
                  {section.label}
                </a>
              </li>
            ))}
          </ul>
          <div className="sheet-cta">
            <ButtonLink variant="ghost" showIcon={false} href="/beta-signup" onClick={() => setOpen(false)}>
              申请内测名额
            </ButtonLink>
          </div>
        </div>
      </div>
      <button
        className={`nav-scrim ${open ? "is-open" : ""}`}
        type="button"
        id="nav-scrim"
        tabIndex={-1}
        aria-hidden="true"
        aria-label="关闭章节菜单"
        onClick={() => setOpen(false)}
      />
    </div>
  );
}
