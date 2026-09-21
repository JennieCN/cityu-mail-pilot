"use client";

import { useEffect } from "react";

export function PageEffects() {
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let revealSlot = 0;

    const io = new IntersectionObserver(
      (entries) => {
        revealSlot = 0;
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const el = entry.target as HTMLElement;
          if (el.classList.contains("b-card")) {
            const delay = revealSlot * 90;
            revealSlot += 1;
            if (delay) {
              el.style.transitionDelay = `${delay}ms`;
              window.setTimeout(() => {
                el.style.transitionDelay = "0ms";
              }, delay + 900);
            }
          }
          el.classList.add("is-in");
          io.unobserve(el);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -10% 0px" },
    );

    document
      .querySelectorAll(".reveal, .rv, .rv-soft, .b-card")
      .forEach((el) => io.observe(el));

    const hero = document.querySelector<HTMLElement>('[data-od-id="hero"]');
    const copy = document.querySelector<HTMLElement>(".hero-copy");
    const visual = document.querySelector<HTMLElement>(".hero-visual");
    const scrubEls = Array.from(document.querySelectorAll<HTMLElement>(".scrub"));
    const topbar = document.querySelector<HTMLElement>('[data-od-id="topbar"]');
    const progressBar = document.getElementById("nav-progress-bar");
    const navLinks = Array.from(document.querySelectorAll<HTMLAnchorElement>('[data-nav]'));
    const sections = ["why", "inbox", "privacy", "faq"]
      .map((id) => document.getElementById(id))
      .filter(Boolean) as HTMLElement[];
    const closing = document.getElementById("closing");

    let frame = 0;
    function tick() {
      frame = 0;
      const vh = window.innerHeight || 1;

      if (hero && !reduced.matches) {
        const rect = hero.getBoundingClientRect();
        const p = Math.max(-0.4, Math.min(1, -rect.top / vh));
        copy?.style.setProperty("--par", `${(p * 12).toFixed(2)}px`);
        visual?.style.setProperty("--par", `${(-p * 26).toFixed(2)}px`);
      }

      if (!reduced.matches) {
        scrubEls.forEach((el) => {
          const rect = el.getBoundingClientRect();
          if (rect.bottom < -60 || rect.top > vh + 60) return;
          let p =
            rect.top < 140
              ? rect.top / 140
              : rect.top > vh * 0.78
                ? (1 - rect.top / vh) / 0.22
                : 1;
          p = Math.max(0, Math.min(1, p));
          const offset = (rect.top < 140 ? -1 : 1) * (1 - p) * 24;
          el.style.opacity = p.toFixed(3);
          el.style.transform = `translate3d(0,${offset.toFixed(2)}px,0)`;
          el.style.filter = p > 0.995 ? "none" : `blur(${((1 - p) * 6).toFixed(2)}px)`;
        });
      }

      const doc = document.documentElement;
      const y = window.pageYOffset || doc.scrollTop || 0;
      const pad = Number.parseFloat(getComputedStyle(doc).scrollPaddingTop) || 112;
      const probe = pad + 40;
      let active: string | null = null;
      sections.forEach((section) => {
        if (section.getBoundingClientRect().top <= probe) active = section.id;
      });
      if (closing && closing.getBoundingClientRect().top <= probe) active = "closing";

      navLinks.forEach((link) => {
        if (active && link.dataset.nav === active) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
      document
        .querySelectorAll<HTMLAnchorElement>(".nav-cta")
        .forEach((link) =>
          active === "closing"
            ? link.setAttribute("aria-current", "true")
            : link.removeAttribute("aria-current"),
        );

      topbar?.classList.toggle("is-scrolled", y > 8);
      if (progressBar) {
        const max = doc.scrollHeight - window.innerHeight;
        const p = max > 0 ? Math.min(1, Math.max(0, y / max)) : 0;
        progressBar.style.transform = `scaleX(${p.toFixed(4)})`;
      }
    }

    function queue() {
      if (!frame) frame = requestAnimationFrame(tick);
    }

    window.addEventListener("scroll", queue, { passive: true });
    window.addEventListener("resize", queue, { passive: true });
    tick();

    const title = document.querySelector<HTMLElement>('[data-od-id="hero-title"]');
    const chars = title ? Array.from(title.querySelectorAll<HTMLElement>(".ch")) : [];
    const lastChar = chars[chars.length - 1];
    const settle = () => title?.classList.add("entered");
    lastChar?.addEventListener("animationend", settle);
    if (reduced.matches) settle();

    const finePointer = window.matchMedia("(pointer:fine)");
    const glassEls = Array.from(
      document.querySelectorAll<HTMLElement>(".nav-shell, .nav-sheet, .shell.preview"),
    );
    const cleanups: Array<() => void> = [];

    if (finePointer.matches && !reduced.matches) {
      glassEls.forEach((el) => {
        let glassFrame = 0;
        let next: { x: string; y: string } | null = null;
        const paint = () => {
          glassFrame = 0;
          if (!next) return;
          el.style.setProperty("--gx", `${next.x}%`);
          el.style.setProperty("--gy", `${next.y}%`);
          next = null;
        };
        const onMove = (event: PointerEvent) => {
          const rect = el.getBoundingClientRect();
          if (!rect.width || !rect.height) return;
          next = {
            x: (((event.clientX - rect.left) / rect.width) * 100).toFixed(2),
            y: (((event.clientY - rect.top) / rect.height) * 100).toFixed(2),
          };
          if (!glassFrame) glassFrame = requestAnimationFrame(paint);
        };
        const onLeave = () => {
          next = null;
          if (glassFrame) cancelAnimationFrame(glassFrame);
          glassFrame = 0;
          el.style.removeProperty("--gx");
          el.style.removeProperty("--gy");
        };
        el.addEventListener("pointermove", onMove, { passive: true });
        el.addEventListener("pointerleave", onLeave);
        cleanups.push(() => {
          el.removeEventListener("pointermove", onMove);
          el.removeEventListener("pointerleave", onLeave);
        });
      });
    }

    return () => {
      io.disconnect();
      window.removeEventListener("scroll", queue);
      window.removeEventListener("resize", queue);
      if (frame) cancelAnimationFrame(frame);
      lastChar?.removeEventListener("animationend", settle);
      cleanups.forEach((cleanup) => cleanup());
    };
  }, []);

  return null;
}
